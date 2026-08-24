"""Fetch the Overpass sidecar's country extract, once, under a hard budget.

This module exists because a deployment of this app downloaded 300+ GB from
Geofabrik and got its IP firewalled. Nothing was malicious: the sidecar's
import was failing, a failed import leaves no database, and the Overpass
container's entrypoint re-downloads the whole extract whenever there is no
database. `restart: unless-stopped` turned that into a loop.

So the download moved here, out of the Overpass container. Three properties
matter, in this order:

1. **The extract is fetched at most once.** A completed file is never fetched
   again, and it lives on a volume of its own so rebuilding the database — the
   documented recovery step — does not throw it away.
2. **A failing fetch stops.** Attempts and bytes are recorded in a ledger next
   to the file. Past the limits this refuses to try, and the init container
   exits non-zero; `depends_on: service_completed_successfully` then means the
   sidecar never starts. One clear error instead of a loop.
3. **The ledger outlives the container.** Any counter kept in a container is
   reset by `docker compose up --force-recreate`, which is exactly what an
   operator does while debugging. This one is on the volume.

Interrupted transfers resume with a Range request rather than starting over,
so the cost of a retry is the part that did not arrive, not the whole file.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from config.overpass_local import (
    DOWNLOAD_BUDGET_MULTIPLIER,
    DOWNLOAD_BUDGET_WINDOW_HOURS,
    DOWNLOAD_CHUNK_BYTES,
    DOWNLOAD_CONNECT_TIMEOUT_S,
    DOWNLOAD_COOLDOWN_HOURS,
    DOWNLOAD_LOCK_STALE_HOURS,
    DOWNLOAD_READ_TIMEOUT_S,
    LEDGER_NAME,
    MAX_DOWNLOAD_ATTEMPTS,
    download_user_agent,
)

logger = logging.getLogger(__name__)

# First bytes of an OSM PBF: a big-endian blob header length followed by a
# protobuf carrying the string "OSMHeader". Cheap way to tell a real extract
# from an error page, a redirect, or a truncated first chunk.
PBF_MAGIC = b"OSMHeader"
PBF_MAGIC_WINDOW = 64

# Below this the file cannot be a country extract; Geofabrik's smallest
# (Iceland) is ~60 MB and the smallest extract anywhere is a few hundred KB.
MIN_PLAUSIBLE_BYTES = 100 * 1024


@dataclass
class FetchOutcome:
    """What happened, in enough detail for the caller to log it and exit."""

    ok: bool
    reason: str
    path: Optional[Path] = None
    downloaded_bytes: int = 0
    already_present: bool = False
    budget_exhausted: bool = False
    retry_after: Optional[datetime] = None


@dataclass
class _Ledger:
    """Attempt history for one extract volume."""

    path: Path
    attempts: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "_Ledger":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            attempts = raw.get("attempts", [])
            if not isinstance(attempts, list):
                attempts = []
        except (OSError, ValueError):
            # A corrupt ledger must not be a free pass to download again, but
            # it also must not wedge a working deployment. Starting from empty
            # is the lesser evil: the file-exists check above still prevents a
            # completed extract from being re-fetched.
            attempts = []
        return cls(path=path, attempts=attempts)

    def save(self) -> None:
        # Keep a month of history; beyond that it is only noise.
        cutoff = _now() - timedelta(days=30)
        self.attempts = [a for a in self.attempts if _parse_ts(a) > cutoff]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"attempts": self.attempts}, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"Could not write the download ledger: {e}")

    def record(self, url: str, downloaded: int, ok: bool, note: str) -> None:
        self.attempts.append(
            {
                "ts": _now().isoformat(),
                "url": url,
                "bytes": downloaded,
                "ok": ok,
                "note": note,
            }
        )
        self.save()

    def _within(self, hours: int) -> list:
        cutoff = _now() - timedelta(hours=hours)
        return [a for a in self.attempts if _parse_ts(a) > cutoff]

    def recent_failures(self) -> list:
        return [a for a in self._within(DOWNLOAD_COOLDOWN_HOURS) if not a.get("ok")]

    def bytes_in_window(self) -> int:
        return sum(
            int(a.get("bytes") or 0) for a in self._within(DOWNLOAD_BUDGET_WINDOW_HOURS)
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(attempt: dict) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(attempt.get("ts", "")))
    except ValueError:
        # An unparseable timestamp is treated as "just now" so a mangled entry
        # counts against the budget rather than being quietly ignored.
        return _now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def looks_like_pbf(path: Path) -> bool:
    """Is this plausibly an OSM PBF rather than an error page?"""
    try:
        if path.stat().st_size < MIN_PLAUSIBLE_BYTES:
            return False
        with path.open("rb") as fh:
            return PBF_MAGIC in fh.read(PBF_MAGIC_WINDOW)
    except OSError:
        return False


class _Lock:
    """Cooperative lock so two init containers cannot fetch at once."""

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            age_h = (time.time() - self.path.stat().st_mtime) / 3600
            if age_h > DOWNLOAD_LOCK_STALE_HOURS:
                logger.warning(
                    f"Clearing a stale download lock ({age_h:.0f}h old) — the "
                    f"container holding it is gone."
                )
                self.path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
        except FileExistsError:
            self.held = False
        except OSError as e:
            # Cannot lock: proceed rather than block a working deployment. The
            # budget still applies, so the worst case stays bounded.
            logger.warning(f"Could not take the download lock: {e}")
            self.held = True
        return self

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


def last_successful_download(cache_dir: Path) -> Optional[datetime]:
    """When this volume last completed a download, from the ledger.

    Used as a rough stand-in for the extract's replication timestamp when the
    PBF itself is gone — after a successful import it is pruned, and its
    headers go with it.
    """
    ledger = _Ledger.load(cache_dir / LEDGER_NAME)
    stamps = [_parse_ts(a) for a in ledger.attempts if a.get("ok")]
    return max(stamps) if stamps else None


def fetch_extract(
    url: str,
    destination: Path,
    expected_gb: float = 0.0,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> FetchOutcome:
    """Make `destination` hold the extract at `url`, downloading at most once.

    Args:
        url: http(s) source. `file://` and local paths are the caller's job —
            nothing here should ever copy bytes it did not fetch.
        destination: final path of the extract.
        expected_gb: published size, used only to size the byte budget.
        on_progress: called with (bytes_so_far, total_or_zero).

    Returns:
        FetchOutcome. `ok=False` is terminal for this run: the caller should
        exit non-zero so the sidecar never starts.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    if looks_like_pbf(destination):
        return FetchOutcome(
            ok=True,
            reason="The extract is already on the volume; nothing to download.",
            path=destination,
            already_present=True,
        )

    ledger = _Ledger.load(destination.parent / LEDGER_NAME)

    failures = ledger.recent_failures()
    if len(failures) >= MAX_DOWNLOAD_ATTEMPTS:
        oldest = min(_parse_ts(a) for a in failures)
        retry_after = oldest + timedelta(hours=DOWNLOAD_COOLDOWN_HOURS)
        return FetchOutcome(
            ok=False,
            budget_exhausted=True,
            retry_after=retry_after,
            reason=(
                f"{len(failures)} failed downloads in the last "
                f"{DOWNLOAD_COOLDOWN_HOURS}h. Refusing to try again before "
                f"{retry_after:%Y-%m-%d %H:%M} UTC — repeatedly re-downloading "
                f"an extract is what gets an IP firewalled. Fix the cause "
                f"first; see TROUBLESHOOTING.md."
            ),
        )

    budget = int((expected_gb or 1.0) * DOWNLOAD_BUDGET_MULTIPLIER * 1e9)
    spent = ledger.bytes_in_window()
    if spent >= budget:
        return FetchOutcome(
            ok=False,
            budget_exhausted=True,
            reason=(
                f"{spent / 1e9:.1f} GB already pulled from the mirror in the "
                f"last {DOWNLOAD_BUDGET_WINDOW_HOURS}h, against a budget of "
                f"{budget / 1e9:.1f} GB. Downloads resume rather than restart, "
                f"so passing this means something is re-sending the whole file "
                f"— stop and investigate rather than trying again."
            ),
        )

    with _Lock(destination.parent / ".download.lock") as lock:
        if not lock.held:
            return FetchOutcome(
                ok=False,
                reason=(
                    "Another container is already downloading this extract. "
                    "Wait for it rather than starting a second transfer."
                ),
            )
        return _download(url, destination, ledger, budget - spent, on_progress)


def _download(
    url: str,
    destination: Path,
    ledger: _Ledger,
    remaining_budget: int,
    on_progress: Optional[Callable[[int, int], None]],
) -> FetchOutcome:
    part = destination.with_suffix(destination.suffix + ".part")
    resume_from = part.stat().st_size if part.is_file() else 0

    headers = {"User-Agent": download_user_agent()}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    transferred = 0
    try:
        timeout = httpx.Timeout(
            DOWNLOAD_READ_TIMEOUT_S, connect=DOWNLOAD_CONNECT_TIMEOUT_S
        )
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 416:
                    # The server says our partial file is already the whole
                    # thing. Promote it and let the magic check decide.
                    part.replace(destination)
                    ok = looks_like_pbf(destination)
                    if not ok:
                        destination.unlink(missing_ok=True)
                    ledger.record(url, 0, ok, "resume returned 416")
                    return FetchOutcome(
                        ok=ok,
                        path=destination if ok else None,
                        reason=(
                            "The partial file was already complete."
                            if ok
                            else "The partial file was complete but is not a PBF."
                        ),
                    )

                if response.status_code >= 400:
                    ledger.record(url, 0, False, f"HTTP {response.status_code}")
                    return FetchOutcome(
                        ok=False,
                        reason=_explain_status(response.status_code, url),
                    )

                # A 200 to a Range request means the server ignored it and is
                # sending the whole file, so the partial is worthless.
                append = resume_from > 0 and response.status_code == 206
                if resume_from and not append:
                    logger.warning(
                        "The mirror ignored the resume request; starting over."
                    )
                    resume_from = 0

                total = _total_bytes(response, resume_from)
                mode = "ab" if append else "wb"
                with part.open(mode) as fh:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                        fh.write(chunk)
                        transferred += len(chunk)
                        if on_progress:
                            on_progress(resume_from + transferred, total)
                        if transferred > remaining_budget:
                            raise _BudgetExceeded()

    except _BudgetExceeded:
        ledger.record(url, transferred, False, "byte budget exceeded mid-transfer")
        return FetchOutcome(
            ok=False,
            budget_exhausted=True,
            downloaded_bytes=transferred,
            reason=(
                f"Stopped mid-transfer at {transferred / 1e9:.1f} GB: the "
                f"download budget for this window is spent. The partial file "
                f"is kept and will resume once the window rolls over."
            ),
        )
    except httpx.HTTPError as e:
        ledger.record(url, transferred, False, f"{type(e).__name__}: {e}")
        return FetchOutcome(
            ok=False,
            downloaded_bytes=transferred,
            reason=(
                f"Download failed after {transferred / 1e6:.0f} MB: "
                f"{type(e).__name__}: {e}. The partial file is kept, so a "
                f"later attempt resumes rather than starting over."
            ),
        )
    except OSError as e:
        ledger.record(url, transferred, False, f"OSError: {e}")
        return FetchOutcome(
            ok=False,
            downloaded_bytes=transferred,
            reason=f"Could not write the extract to disk: {e}",
        )

    final_size = part.stat().st_size if part.is_file() else 0
    if not looks_like_pbf(part):
        # Do not keep a partial that is not even a PBF — resuming from an error
        # page would append real data to HTML and corrupt the result.
        part.unlink(missing_ok=True)
        ledger.record(url, transferred, False, "not a PBF")
        return FetchOutcome(
            ok=False,
            downloaded_bytes=transferred,
            reason=(
                f"The mirror returned {final_size} bytes that are not an OSM "
                f"PBF — usually an error page. Discarded; check {url}."
            ),
        )

    part.replace(destination)
    ledger.record(url, transferred, True, "complete")
    return FetchOutcome(
        ok=True,
        path=destination,
        downloaded_bytes=transferred,
        reason=f"Downloaded {final_size / 1e9:.2f} GB.",
    )


class _BudgetExceeded(Exception):
    """Raised inside the stream loop to unwind to the budget handler."""


def _total_bytes(response: httpx.Response, resume_from: int) -> int:
    """Full size of the extract, or 0 when the mirror does not say."""
    if response.status_code == 206:
        content_range = response.headers.get("content-range", "")
        if "/" in content_range:
            try:
                return int(content_range.rsplit("/", 1)[1])
            except ValueError:
                pass
    length = response.headers.get("content-length")
    if length:
        try:
            return int(length) + (resume_from if response.status_code == 206 else 0)
        except ValueError:
            pass
    return 0


def _explain_status(status: int, url: str) -> str:
    if status == 404:
        return (
            f"The mirror has no extract at {url} (HTTP 404). Check "
            f"OVERPASS_LOCAL_COUNTRIES / OVERPASS_LOCAL_MIRROR — not every "
            f"mirror publishes every country."
        )
    if status == 403:
        return (
            f"The mirror refused the request (HTTP 403) for {url}. If this is "
            f"Geofabrik, the IP may be blocked; set OVERPASS_LOCAL_MIRROR=osmfr "
            f"and see TROUBLESHOOTING.md."
        )
    if status == 429:
        return (
            f"The mirror is rate-limiting this IP (HTTP 429). Wait rather than "
            f"retrying — retrying through a 429 is how a rate limit becomes a "
            f"block."
        )
    return f"The mirror answered HTTP {status} for {url}."
