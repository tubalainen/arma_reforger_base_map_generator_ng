"""On-disk cache for raw Overpass API responses.

Regenerating the same square is the common case — the user tweaks a setting,
or a later pipeline step failed and the job is re-run. Overpass is the slowest
and least reliable step in the pipeline, so caching its raw payload turns a
repeat generation into an instant one and puts zero load on the volunteer-run
mirrors.

The cache key is a hash of the exact query string, which already encodes the
bbox (queries are built with `[bbox:...]`). Entries are gzipped JSON with an
mtime-based TTL; a size cap evicts oldest-first.

Every failure here is non-fatal: a miss, a corrupt entry, or an unwritable
cache directory all degrade to "fetch it from the network".
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from config import (
    OVERPASS_CACHE_ENABLED,
    OVERPASS_CACHE_TTL_HOURS,
    OVERPASS_CACHE_MAX_MB,
)
from config.paths import BASE_DIR

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "output" / ".overpass_cache"


def _cache_key(query: str) -> str:
    """Stable key for a query string.

    Whitespace is normalised first so that cosmetic changes to the query
    template's indentation don't invalidate every entry.
    """
    normalised = " ".join(query.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:32]


def _entry_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json.gz"


def load(query: str) -> Optional[dict]:
    """Return the cached payload for `query`, or None on any miss."""
    if not OVERPASS_CACHE_ENABLED:
        return None

    path = _entry_path(_cache_key(query))
    try:
        if not path.is_file():
            return None

        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > OVERPASS_CACHE_TTL_HOURS:
            logger.debug(f"Overpass cache: entry {path.name} expired ({age_hours:.1f}h)")
            path.unlink(missing_ok=True)
            return None

        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)

        logger.info(
            f"Overpass cache HIT ({len(payload.get('elements', []))} elements, "
            f"{age_hours:.1f}h old) — skipping network fetch"
        )
        return payload
    except Exception as e:
        # A corrupt or half-written entry must never break a generation.
        logger.warning(f"Overpass cache: unusable entry {path.name} ({type(e).__name__}), ignoring")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def store(query: str, payload: dict) -> None:
    """Cache a successful Overpass payload. Failures are logged and ignored."""
    if not OVERPASS_CACHE_ENABLED:
        return

    path = _entry_path(_cache_key(query))
    tmp = path.with_suffix(".tmp")
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename so a crash mid-write can't leave a
        # truncated entry that later reads would have to recover from.
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)
        logger.debug(f"Overpass cache: stored {path.name} ({path.stat().st_size / 1024:.1f} KB)")
        _evict_if_over_cap()
    except Exception as e:
        logger.warning(f"Overpass cache: could not store entry ({type(e).__name__}), continuing")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _evict_if_over_cap() -> None:
    """Drop the oldest entries until the cache fits inside its size cap."""
    try:
        entries = sorted(CACHE_DIR.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in entries)
        cap = OVERPASS_CACHE_MAX_MB * 1024 * 1024
        if total <= cap:
            return

        removed = 0
        for path in entries:
            if total <= cap:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        logger.info(f"Overpass cache: evicted {removed} oldest entr(ies) to stay under {OVERPASS_CACHE_MAX_MB} MB")
    except Exception as e:
        logger.debug(f"Overpass cache: eviction skipped ({type(e).__name__})")
