"""Seed the Overpass sidecar's replication state so its diff loop can start.

Overpass tracks where it is in a mirror's diff stream with a single file,
`/db/replicate_id`, holding one sequence number. Without it the update loop
cannot run — and it cannot create the file either, so a sidecar that never got
one never updates again. Silently: it keeps answering queries perfectly with
data that quietly ages.

The upstream image seeds it once, during the import, by running
`pyosmium-get-changes -O <planet file>`. That has two failure modes we have
now watched happen:

1. A momentary network fault during that one call loses it permanently. The
   script's last line is `) 2>&1 | tee -a /db/changes.log` with no `pipefail`,
   so the pipeline reports `tee`'s exit status — always zero. The entrypoint
   sees success, writes `/db/init_done`, and the sidecar is left with a dead
   update loop and no way back.
2. `-O` reads `osmosis_replication_*` from the planet file's header, and those
   headers do not survive the PBF -> XML conversion the importer forces on us.
   pyosmium falls back to scanning the entire file for its newest object, which
   costs minutes and yields a timestamp rather than a sequence.

So we seed it ourselves, before the sidecar starts, from the PBF while its
headers are intact. Seeding by *timestamp* rather than by the header's sequence
number is deliberate: sequence numbers are meaningful only on the mirror that
issued them, and switching mirrors must not silently resume at some unrelated
point in a different stream.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from config.overpass_local import (
    DOWNLOAD_CONNECT_TIMEOUT_S,
    download_user_agent,
)

logger = logging.getLogger(__name__)

# Replication servers publish a state file per sequence under a three-level
# zero-padded path: sequence 7256890 -> 007/256/890.state.txt. Both Geofabrik
# and OSM France use this osmosis layout.
STATE_TIMEOUT_S = 20.0

# Ceiling on probes while locating a sequence. A binary search over a minutely
# stream needs about 23; anything beyond this means the server is not laid out
# the way we think and we should stop poking it.
MAX_STATE_PROBES = 32

_SEQ_RE = re.compile(r"^sequenceNumber\s*=\s*(\d+)", re.MULTILINE)
_TS_RE = re.compile(r"^timestamp\s*=\s*(.+)$", re.MULTILINE)


@dataclass
class SeedOutcome:
    ok: bool
    reason: str
    sequence: Optional[int] = None
    already_present: bool = False


def _parse_state(text: str) -> tuple[Optional[int], Optional[datetime]]:
    """Pull the sequence and timestamp out of an osmosis state file.

    Timestamps arrive with their colons backslash-escaped, because the format
    is a Java properties file.
    """
    seq_match = _SEQ_RE.search(text)
    ts_match = _TS_RE.search(text)
    seq = int(seq_match.group(1)) if seq_match else None

    stamp: Optional[datetime] = None
    if ts_match:
        raw = ts_match.group(1).strip().replace("\\", "").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
            stamp = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            stamp = None
    return seq, stamp


def _state_path(sequence: int) -> str:
    """Relative path of a sequence's state file, e.g. 007/256/890.state.txt."""
    padded = f"{sequence:09d}"
    return f"{padded[0:3]}/{padded[3:6]}/{padded[6:9]}.state.txt"


class _StateReader:
    """Fetches state files from one replication server, counting probes."""

    def __init__(self, diff_url: str, client: httpx.Client):
        self.base = diff_url.rstrip("/") + "/"
        self.client = client
        self.probes = 0

    def read(self, path: str) -> tuple[Optional[int], Optional[datetime]]:
        if self.probes >= MAX_STATE_PROBES:
            raise RuntimeError(
                f"Gave up after {self.probes} state-file requests — the "
                f"replication server is not laid out as expected."
            )
        self.probes += 1
        response = self.client.get(self.base + path)
        if response.status_code != 200:
            return None, None
        return _parse_state(response.text)


def replication_timestamp_from_pbf(pbf: Path) -> Optional[datetime]:
    """Read `osmosis_replication_timestamp` from a PBF's header.

    Only PBF carries this; it is lost in the conversion to XML, which is why
    this has to happen before the extract is converted.
    """
    try:
        result = subprocess.run(
            [
                "osmium",
                "fileinfo",
                "-g",
                "header.option.osmosis_replication_timestamp",
                str(pbf),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"Could not read replication headers from {pbf}: {e}")
        return None

    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sequence_for_timestamp(diff_url: str, target: datetime) -> Optional[int]:
    """Largest sequence on `diff_url` whose data is no newer than `target`.

    Binary search over the server's per-sequence state files. Erring on the
    early side is deliberate: resuming a little before the database's own
    timestamp re-applies changes it already has, which is harmless, whereas
    resuming late leaves a permanent hole in the data.
    """
    timeout = httpx.Timeout(STATE_TIMEOUT_S, connect=DOWNLOAD_CONNECT_TIMEOUT_S)
    headers = {"User-Agent": download_user_agent()}
    try:
        with httpx.Client(
            timeout=timeout, headers=headers, follow_redirects=True
        ) as client:
            reader = _StateReader(diff_url, client)
            newest_seq, newest_ts = reader.read("state.txt")
            if newest_seq is None:
                logger.warning(f"No usable state.txt at {diff_url}")
                return None
            if newest_ts is not None and newest_ts <= target:
                return newest_seq

            lo, hi, best = 0, newest_seq, 0
            while lo <= hi:
                mid = (lo + hi) // 2
                _, stamp = reader.read(_state_path(mid))
                if stamp is None:
                    # Sequences below the server's retention have no state
                    # file. Treat the gap as "too old" and search upward.
                    lo = mid + 1
                    continue
                if stamp <= target:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best or None
    except (httpx.HTTPError, RuntimeError) as e:
        logger.warning(f"Could not locate a replication sequence: {e}")
        return None


def seed(
    sequence_file: Path,
    diff_url: str,
    target: Optional[datetime],
) -> SeedOutcome:
    """Ensure `sequence_file` holds a sequence valid for `diff_url`.

    Leaves an existing file alone — once the sidecar is running it owns that
    file, and overwriting it would rewind or skip the update stream.
    """
    try:
        if sequence_file.is_file() and sequence_file.read_text().strip():
            return SeedOutcome(
                ok=True,
                already_present=True,
                reason="Replication state already present; leaving it alone.",
            )
    except OSError as e:
        return SeedOutcome(ok=False, reason=f"Cannot read {sequence_file}: {e}")

    if not diff_url:
        return SeedOutcome(
            ok=True,
            reason="No diff URL configured, so there is no update loop to seed.",
        )
    if target is None:
        return SeedOutcome(
            ok=False,
            reason=(
                "No replication timestamp available for this extract, so the "
                "update loop cannot be given a starting point."
            ),
        )

    sequence = sequence_for_timestamp(diff_url, target)
    if sequence is None:
        return SeedOutcome(
            ok=False,
            reason=(
                f"Could not find a sequence at or before {target:%Y-%m-%d %H:%M} "
                f"UTC on {diff_url}."
            ),
        )

    try:
        sequence_file.parent.mkdir(parents=True, exist_ok=True)
        sequence_file.write_text(f"{sequence}\n", encoding="utf-8")
    except OSError as e:
        return SeedOutcome(ok=False, reason=f"Cannot write {sequence_file}: {e}")

    return SeedOutcome(
        ok=True,
        sequence=sequence,
        reason=(
            f"Seeded the update loop at sequence {sequence} "
            f"({target:%Y-%m-%d %H:%M} UTC)."
        ),
    )
