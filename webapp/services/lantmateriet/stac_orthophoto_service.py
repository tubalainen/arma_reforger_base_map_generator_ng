"""
Lantmäteriet STAC Bild (orthophoto) service.

Fetches the most recent aerial orthophoto for Swedish terrain from the
Lantmäteriet STAC Bild API. Replaces the legacy WMS historical orthophoto
service (imagery dated 2005) with near-current imagery (2007–2025, 0.16 m/px).

Architecture notes:
- STAC catalog (api.lantmateriet.se/stac-bild/v1/) is OPEN — no auth for search.
- Asset downloads (dl1.lantmateriet.se) require HTTP Basic Auth (same credentials
  as the STAC Höjd elevation service).
- Tiles are ~2500 m × 2500 m COG GeoTIFF in EPSG:3006, RGBI (4 bands), ~460 MB each.
- Full tile downloads are impractical. Instead, GDAL VSICURL is used to open COG
  files directly over HTTPS; rasterio.merge() with a bounds constraint issues HTTP
  range requests for only the pixels we need (~2–5 MB per tile for a 5 km area).

Workflow:
1. POST /stac-bild/v1/search (open, no auth) — newest items first
2. Build VSICURL paths for the "data" asset of each matching item
3. Open all COGs in parallel via rasterio.Env(GDAL_HTTP_AUTH=BASIC, …) +
   /vsicurl/ prefix (ThreadPoolExecutor, capped at 8 workers — small
   round-trips, server tolerates the burst)
4. Read each tile's windowed intersection with the target bbox in two
   phases (RGB bands only — drops NIR band 4 — using COG overviews):
   Phase 1 = 4 parallel workers, single attempt per tile, failures
   deferred to Phase 2. Phase 2 = single-threaded serial retry sweep
   with jittered backoff (1–5s) so retries can't stampede or collide
   with still-running Phase 1 reads. Both phases share a daemon
   watchdog thread that emits heartbeat progress, flags stalled tiles
   (>60s in flight), and enforces a 10-min total wall-clock cap.
   Then first-wins merge in newest-first order so newer imagery takes
   priority. v1.5.6 + refs #118 — replaces the v1.5.5 in-worker retry
   loop that caused thrashing under dl1.lantmateriet.se's connection
   drops.
5. Warp merged EPSG:3006 result to WGS84 at the requested pixel dimensions
   (single multi-band reproject with GDAL num_threads, all 3 bands at once)
6. Return PNG bytes — same format as the WMS service, so the existing
   reprojection step (step 7b in map_generator) aligns it correctly
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx
import numpy as np

from config.lantmateriet import LANTMATERIET_CONFIG

logger = logging.getLogger(__name__)


def _default_workers() -> int:
    """Worker count for parallel tile I/O — matches services.utils.parallel."""
    return min(8, os.cpu_count() or 2)


def _default_read_workers() -> int:
    """Worker count for Phase 1 (parallel single-attempt) tile reads.

    Capped at 4 because dl1.lantmateriet.se drops connections under
    sustained range-read load — v1.5.5 at 6 workers still triggered
    ~30% per-attempt failure rates. Phase 2 picks up any failures
    serially, so we trade peak throughput for predictable success.
    """
    return min(4, os.cpu_count() or 2)


def _serial_retry_attempts() -> int:
    """Phase 2 serial retry budget per failed tile.

    Each retry is jitter-spaced (1–4s) and runs single-threaded, so
    there is no stampede effect. Production data showed tiles needing
    up to 3 attempts before recovering, so 3 is the right ceiling.
    """
    return 3


def _gdal_quiet_env() -> dict:
    """Extra GDAL env vars layered on top of _gdal_vsicurl_env() for the
    merge phase.

    GDAL emits raw stderr lines like
    `ERROR 1: Request for <range> failed with response_code=0` and
    decode chatter (`ZIPDecode: unknown compression method`) for every
    individual byte-range request that gets a dropped connection.
    Those events are already handled by the merge orchestrator;
    surfacing them at the GDAL layer just floods the log without
    adding diagnostic value. The orchestrator emits one concise line
    per per-tile event instead.
    """
    return {
        # Suppress the GDAL CPL debug stream entirely.
        "CPL_DEBUG": "OFF",
        # Quiet libcurl's per-request error chatter.
        "CPL_CURL_VERBOSE": "NO",
        # Redirect GDAL's CPL_LOG to a sink so any errors GDAL would
        # otherwise print to stderr go to /dev/null instead.
        "CPL_LOG": "NUL" if os.name == "nt" else "/dev/null",
    }


@contextlib.contextmanager
def _quiet_rasterio_logging():
    """Temporarily raise rasterio's GDAL-bridge loggers to WARNING.

    Rasterio installs a Python error handler that surfaces every GDAL
    `CPLError` as an `INFO` log line on `rasterio._err`. During a flaky
    merge those duplicate every connection drop and decode error 3-5
    times per failed tile attempt. The merge orchestrator already
    logs one concise line per outcome, so we silence the duplicate
    chatter for the merge duration and restore on exit.
    """
    targets = ("rasterio._err", "rasterio._io")
    saved = {name: logging.getLogger(name).level for name in targets}
    for name in targets:
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


class _MergeWatchdog:
    """Daemon thread that monitors merge progress and flags stalled tiles.

    Replaces the v1.5.4 heartbeat thread with a richer observer. It
    tracks per-attempt start times so any tile read that exceeds
    `stall_seconds` (default 60s) is logged as stalled — useful when
    the upstream server is silently rate-limiting and a connection
    just hangs.

    Watchdog is purely observational: Python threads cannot be killed,
    so a "stalled" tile keeps running until GDAL_HTTP_TIMEOUT (60s)
    eventually trips. The watchdog surfaces the condition so the user
    knows what's happening, not so we can preempt the read.
    """

    def __init__(
        self,
        total_tiles: int,
        job=None,
        interval: float = 5.0,
        stall_seconds: float = 60.0,
        total_timeout_seconds: float = 600.0,
    ):
        self.total = total_tiles
        self.job = job
        self.interval = interval
        self.stall_seconds = stall_seconds
        self.total_timeout_seconds = total_timeout_seconds

        self._lock = threading.Lock()
        # idx -> attempt start time (current attempt only)
        self._in_flight: dict[int, float] = {}
        # idx values that are fully done (either succeeded or gave up)
        self._tiles_done: set[int] = set()
        self._attempts_failed = 0
        # Stall warnings deduplicated per idx so we don't spam every tick.
        self._stall_warned: set[int] = set()

        self._stop = threading.Event()
        self._timed_out = threading.Event()
        self._t0 = time.monotonic()
        self._thread: Optional[threading.Thread] = None

    def begin(self, idx: int) -> None:
        with self._lock:
            self._in_flight[idx] = time.monotonic()
            self._stall_warned.discard(idx)

    def end_ok(self, idx: int) -> None:
        with self._lock:
            self._in_flight.pop(idx, None)
            self._tiles_done.add(idx)

    def end_attempt_fail(self, idx: int) -> None:
        with self._lock:
            self._in_flight.pop(idx, None)
            self._attempts_failed += 1

    def end_perm_fail(self, idx: int) -> None:
        with self._lock:
            self._in_flight.pop(idx, None)
            self._tiles_done.add(idx)

    @property
    def timed_out(self) -> bool:
        return self._timed_out.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            elapsed = now - self._t0
            with self._lock:
                in_flight = dict(self._in_flight)
                done = len(self._tiles_done)
                attempts_failed = self._attempts_failed
                stall_warned = self._stall_warned

            stalled = [
                (i, now - t) for i, t in in_flight.items()
                if now - t > self.stall_seconds and i not in stall_warned
            ]
            for i, age in stalled:
                logger.warning(
                    f"STAC Bild: tile {i + 1} stalled — {age:.0f}s in flight "
                    f"(GDAL_HTTP_TIMEOUT will trip at 60s)"
                )
                with self._lock:
                    self._stall_warned.add(i)

            logger.info(
                f"STAC Bild: watchdog — {done}/{self.total} done, "
                f"{len(in_flight)} in-flight, {attempts_failed} attempt(s) failed "
                f"({elapsed:.0f}s elapsed)"
            )
            if self.job:
                self.job.add_log(
                    f"...orthophoto progress: {done}/{self.total} done, "
                    f"{len(in_flight)} in-flight, {attempts_failed} attempt(s) "
                    f"failed ({elapsed:.0f}s)"
                )

            if elapsed > self.total_timeout_seconds:
                logger.error(
                    f"STAC Bild: watchdog — merge exceeded "
                    f"{self.total_timeout_seconds:.0f}s wall-clock cap; "
                    f"signalling abort"
                )
                self._timed_out.set()
                return

# STAC search endpoint (open — no auth required)
_SEARCH_HEADERS = {
    "User-Agent": "ArmaReforgerMapGenerator/1.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _wgs84_bbox_to_epsg3006_envelope(
    bbox_wgs84: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """
    Convert a WGS84 (west, south, east, north) bbox to the EPSG:3006 envelope
    that fully contains its four corners. See fetch_stac_orthophoto() for why
    SW+NE-only is insufficient.
    """
    from pyproj import Transformer

    w, s, e, n = bbox_wgs84
    to_3006 = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True)
    corner_lons = [w, e, e, w]
    corner_lats = [s, s, n, n]
    xs, ys = to_3006.transform(corner_lons, corner_lats)
    return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))


def _gdal_vsicurl_env() -> dict:
    """
    Return GDAL environment variables for authenticated VSICURL access.

    GDAL uses these at the C level to attach Basic Auth to every HTTP
    range request made by rasterio when opening /vsicurl/ paths.
    """
    return {
        "GDAL_HTTP_AUTH": "BASIC",
        "GDAL_HTTP_USERPWD": (
            f"{LANTMATERIET_CONFIG.username}:{LANTMATERIET_CONFIG.password}"
        ),
        "GDAL_HTTP_TIMEOUT": "60",
        # Disable directory listing — we know exactly which files we want
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        # Allow COG range reads on .tif/.tiff files
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
        # Merge consecutive ranges into a single HTTP request for speed
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        # Retry individual range requests inside one read when dl1.lantmateriet.se
        # drops the connection mid-flight (observed as response_code=0 →
        # ZIPDecode: unknown compression method). GDAL retries the HTTP
        # request at the curl layer before bubbling the failure up.
        "GDAL_HTTP_MAX_RETRY": "3",
        "GDAL_HTTP_RETRY_DELAY": "1",
    }


def _cog_merge_rgb(
    vsicurl_hrefs: list[str],
    epsg3006_bounds: tuple[float, float, float, float],
    target_width: int,
    target_height: int,
    job=None,
) -> tuple[Optional[np.ndarray], object]:
    """
    Open COG tiles via VSICURL and merge the RGB bands over the target bbox.

    Uses rasterio.merge() with a bounds constraint so GDAL issues HTTP range
    requests only for the pixels we need. The merged array preserves the
    source COG's native resolution; the caller resamples to the final size
    in the EPSG:3006 → WGS84 warp step.

    Why no explicit `res=`: passing a target resolution computed as
    `(x_max - x_min) / target_width` almost never matches the COG's native
    pixel size, so rasterio resamples each source tile separately into the
    target grid. Because each tile's pixel grid origin is independent, the
    fractional offset can leave 1-pixel-wide unfilled columns between tiles —
    which `nodata=0` then renders as the black streak in issue #65. Using
    the source's native resolution keeps tile windows on a single shared
    pixel grid and removes the seam.

    Args:
        vsicurl_hrefs: List of '/vsicurl/https://dl1.lantmateriet.se/...' paths.
        epsg3006_bounds: (x_min, y_min, x_max, y_max) in EPSG:3006 (metres).
        target_width: Output pixel width (used only for the overview hint).
        target_height: Output pixel height.

    Returns:
        (merged_rgb_array, affine_transform) where the array has shape
        (3, H, W) in uint8. Returns (None, None) on failure.
    """
    import rasterio
    from affine import Affine
    from rasterio.crs import CRS
    from rasterio.env import Env
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import from_bounds as window_from_bounds

    x_min, y_min, x_max, y_max = epsg3006_bounds
    n_hrefs = len(vsicurl_hrefs)
    target_crs = CRS.from_epsg(3006)
    workers = _default_workers()
    read_workers = _default_read_workers()
    max_serial_retries = _serial_retry_attempts()

    # --------------------------------------------------------------
    # 1. Parallel COG header opens
    #
    # Each rasterio.open(/vsicurl/...) is a small HTTP range-request
    # round-trip; doing them serially scales with len(hrefs). Open in
    # threads so we trade ~50 sequential round-trips for ~50/workers.
    # Results are reassembled in input order so the merge's
    # "first-wins / newest-first" semantics are preserved.
    # --------------------------------------------------------------
    open_results: list[Optional[tuple]] = [None] * n_hrefs

    def _open_one(idx_href: tuple[int, str]) -> tuple[int, Optional[object], Optional[object], Optional[str]]:
        """Open one COG, decide whether to skip or WarpedVRT-wrap it.

        Returns (idx, dataset_for_merge, source_handle, log_line).
        dataset_for_merge is None if the tile was skipped or failed.
        source_handle is the underlying rasterio handle to close later;
        if it equals dataset_for_merge, no VRT is in play.
        """
        idx, href = idx_href
        with Env(**_gdal_vsicurl_env()):
            try:
                ds = rasterio.open(href)
            except Exception as exc:
                return idx, None, None, f"Could not open COG {href}: {exc}"
            # STAC Bild's primary collection is EPSG:3006 4-band RGBI, but
            # sibling collections (e.g. se1g — historic single-band
            # panchromatic) sometimes surface in the same search result.
            # A windowed multi-band read on a 1-band tile would raise
            # "band index out of range" and abort the merge. Skip those.
            if ds.count < 3:
                return (
                    idx,
                    None,
                    ds,
                    f"STAC Bild: skipping COG {idx + 1}/{n_hrefs} — "
                    f"{ds.count}-band source (need ≥3 for RGB merge): {href}",
                )
            # Sibling collections occasionally surface in a different CRS;
            # wrap mismatched tiles in a WarpedVRT that lazily reprojects
            # to EPSG:3006 on read so all tiles share one CRS.
            if ds.crs != target_crs:
                vrt = WarpedVRT(ds, crs=target_crs, resampling=Resampling.bilinear)
                return (
                    idx,
                    vrt,
                    ds,
                    f"STAC Bild: opened COG {idx + 1}/{n_hrefs} "
                    f"({ds.width}×{ds.height} px native, "
                    f"reprojecting {ds.crs.to_string()} → EPSG:3006)",
                )
            return (
                idx,
                ds,
                ds,
                f"STAC Bild: opened COG {idx + 1}/{n_hrefs} "
                f"({ds.width}×{ds.height} px native)",
            )

    open_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_open_one, (i, h)) for i, h in enumerate(vsicurl_hrefs)]
        for fut in as_completed(futures):
            entry = fut.result()
            open_results[entry[0]] = entry

    # Assemble in newest-first input order — "first wins" depends on it.
    datasets: list = []
    source_handles: list = []
    skipped_single_band = 0
    for entry in open_results:
        if entry is None:
            continue
        _, ds_for_merge, src_handle, log_line = entry
        if log_line:
            logger.info(log_line)
        if src_handle is not None and ds_for_merge is None:
            # opened but skipped (e.g. <3 bands); still close later
            skipped_single_band += 1
            source_handles.append(src_handle)
            continue
        if ds_for_merge is None:
            # open failed
            continue
        datasets.append(ds_for_merge)
        source_handles.append(src_handle)

    open_elapsed = time.monotonic() - open_start
    skip_note = (
        f" ({skipped_single_band} skipped as <3-band)"
        if skipped_single_band else ""
    )
    logger.info(
        f"STAC Bild: opened {len(datasets)}/{n_hrefs} COG headers "
        f"in {open_elapsed:.1f}s{skip_note} (workers={workers})"
    )
    if job:
        job.add_log(
            f"Opened {len(datasets)}/{n_hrefs} Lantmäteriet COG tile headers "
            f"in {open_elapsed:.1f}s — starting parallel windowed read..."
        )

    if not datasets:
        for h in source_handles:
            try:
                h.close()
            except Exception:
                pass
        return None, None

    # Hint GDAL toward a coarse overview level when one would suffice:
    # pick a target resolution that yields roughly `target_width` pixels at
    # the requested bounds. Each tile read passes out_shape=, so GDAL picks
    # the closest source overview whose resolution is finer or equal —
    # tile windows stay grid-aligned across tiles.
    approx_res = max(
        (x_max - x_min) / max(target_width, 1),
        (y_max - y_min) / max(target_height, 1),
    )

    # Snap bounds to integer multiples of approx_res (equivalent to
    # rasterio.merge's target_aligned_pixels=True) so every tile resamples
    # into the same shared pixel grid — no sub-pixel slivers between tiles.
    x_min_s = math.floor(x_min / approx_res) * approx_res
    y_min_s = math.floor(y_min / approx_res) * approx_res
    x_max_s = math.ceil(x_max / approx_res) * approx_res
    y_max_s = math.ceil(y_max / approx_res) * approx_res

    out_w = max(1, int(round((x_max_s - x_min_s) / approx_res)))
    out_h = max(1, int(round((y_max_s - y_min_s) / approx_res)))
    out_transform = Affine.translation(x_min_s, y_max_s) * Affine.scale(
        approx_res, -approx_res
    )

    logger.info(
        f"STAC Bild: merging {len(datasets)} tiles at "
        f"approx_res={approx_res:.2f} m/px over bounds "
        f"{int(x_max - x_min)}×{int(y_max - y_min)} m "
        f"→ output {out_w}×{out_h} px "
        f"(phase 1: {read_workers} parallel workers, "
        f"phase 2: up to {max_serial_retries} serial retries)"
    )
    if job:
        job.add_log(
            f"Reading {len(datasets)} orthophoto tile windows: "
            f"phase 1 parallel ({read_workers} workers), "
            f"phase 2 serial retry for any failures..."
        )

    merge_start = time.monotonic()
    merged = np.zeros((3, out_h, out_w), dtype=np.uint8)

    # Per-tile result slot. Populated by either Phase 1 (parallel) or
    # Phase 2 (serial retry). Tiles that overlap the output bbox but
    # legitimately have no pixels in range stay None — same as before.
    results: list[Optional[tuple]] = [None] * len(datasets)

    watchdog = _MergeWatchdog(len(datasets), job=job)
    watchdog.start()

    # _read_tile_once returns one of three outcome tuples:
    #   ("ok",   idx, rs, re, cs, ce, buf, elapsed)
    #   ("skip", idx)                              — tile doesn't overlap
    #   ("fail", idx, exc_repr)                    — read raised, not retried here
    def _read_tile_once(idx_ds: tuple[int, object]) -> tuple:
        idx, ds = idx_ds
        watchdog.begin(idx)
        t0 = time.monotonic()
        # Layer the merge-only quiet env on top of the auth env. Per-worker
        # GDAL_NUM_THREADS=2 lets each tile's own decode use 2 GDAL
        # threads; bounded total ≈ workers × 2 stays sane.
        env = {
            **_gdal_vsicurl_env(),
            **_gdal_quiet_env(),
            "GDAL_NUM_THREADS": "2",
        }
        try:
            with Env(**env):
                tx_min, ty_min, tx_max, ty_max = ds.bounds
                ix_min = max(x_min_s, tx_min)
                ix_max = min(x_max_s, tx_max)
                iy_min = max(y_min_s, ty_min)
                iy_max = min(y_max_s, ty_max)
                if ix_min >= ix_max or iy_min >= iy_max:
                    return ("skip", idx)

                col_start = int(round((ix_min - x_min_s) / approx_res))
                col_end = int(round((ix_max - x_min_s) / approx_res))
                row_start = int(round((y_max_s - iy_max) / approx_res))
                row_end = int(round((y_max_s - iy_min) / approx_res))
                col_end = min(col_end, out_w)
                row_end = min(row_end, out_h)
                dest_h = row_end - row_start
                dest_w = col_end - col_start
                if dest_h <= 0 or dest_w <= 0:
                    return ("skip", idx)

                src_window = window_from_bounds(
                    ix_min, iy_min, ix_max, iy_max, transform=ds.transform
                )
                buf = ds.read(
                    indexes=[1, 2, 3],
                    window=src_window,
                    out_shape=(3, dest_h, dest_w),
                    resampling=Resampling.bilinear,
                    boundless=True,
                    fill_value=0,
                )
            return (
                "ok",
                idx,
                row_start,
                row_end,
                col_start,
                col_end,
                buf,
                time.monotonic() - t0,
            )
        except Exception as exc:
            # Short-form so the log line stays readable. The full GDAL
            # error is in CPL_LOG anyway — for live debugging set
            # CPL_DEBUG=ON via env to restore the verbose stream.
            exc_repr = type(exc).__name__
            if str(exc):
                exc_repr = f"{exc_repr}: {str(exc).splitlines()[0][:120]}"
            return ("fail", idx, exc_repr)

    try:
        with _quiet_rasterio_logging():
            # ----- Phase 1 — bounded parallel, single attempt per tile.
            phase1_start = time.monotonic()
            logger.info(
                f"STAC Bild: phase 1/2 — parallel single-attempt read "
                f"({read_workers} workers, {len(datasets)} tiles)"
            )
            if job:
                job.add_log(
                    f"Phase 1: parallel read of {len(datasets)} tiles "
                    f"with {read_workers} workers..."
                )

            failed_in_phase1: list[tuple[int, object]] = []
            phase1_ok = 0
            with ThreadPoolExecutor(max_workers=read_workers) as pool:
                fut_to_idx_ds = {
                    pool.submit(_read_tile_once, (i, ds)): (i, ds)
                    for i, ds in enumerate(datasets)
                }
                for fut in as_completed(fut_to_idx_ds):
                    if watchdog.timed_out:
                        # Cancel still-pending futures; running ones will
                        # finish on their own. Either way we stop draining.
                        for pending in fut_to_idx_ds:
                            if not pending.done():
                                pending.cancel()
                        break
                    outcome = fut.result()
                    kind = outcome[0]
                    idx = outcome[1]
                    if kind == "ok":
                        results[idx] = outcome[1:]   # (idx, rs, re, cs, ce, buf, elapsed)
                        watchdog.end_ok(idx)
                        phase1_ok += 1
                    elif kind == "skip":
                        # Doesn't overlap output — count as done, no fail.
                        watchdog.end_ok(idx)
                    else:  # "fail"
                        watchdog.end_attempt_fail(idx)
                        failed_in_phase1.append(fut_to_idx_ds[fut])
                        # One concise log line per failure replaces the
                        # 5-15 lines of GDAL chatter we used to emit.
                        logger.info(
                            f"STAC Bild: tile {idx + 1} → phase 2 "
                            f"({outcome[2]})"
                        )

            phase1_elapsed = time.monotonic() - phase1_start
            logger.info(
                f"STAC Bild: phase 1 done in {phase1_elapsed:.1f}s — "
                f"{phase1_ok} ok, {len(failed_in_phase1)} deferred"
            )
            if job:
                job.add_log(
                    f"Phase 1 complete in {phase1_elapsed:.1f}s — "
                    f"{phase1_ok} ok, {len(failed_in_phase1)} deferred to phase 2"
                )

            # ----- Phase 2 — serial retry sweep with jittered backoff.
            permanent_failures = 0
            if failed_in_phase1 and not watchdog.timed_out:
                phase2_start = time.monotonic()
                logger.info(
                    f"STAC Bild: phase 2/2 — serial retry sweep "
                    f"({len(failed_in_phase1)} tiles, up to "
                    f"{max_serial_retries} attempts each, jittered backoff)"
                )
                if job:
                    job.add_log(
                        f"Phase 2: retrying {len(failed_in_phase1)} tile(s) "
                        f"serially with jittered backoff..."
                    )

                recovered = 0
                for idx, ds in failed_in_phase1:
                    if watchdog.timed_out:
                        break
                    success = False
                    for attempt in range(max_serial_retries):
                        # Jittered linear backoff: 1.0–2.5s, 1.0–4.0s, 1.0–5.5s.
                        # First attempt waits too — server needs to cool off
                        # from Phase 1's load before we hit it again.
                        sleep_for = 1.0 + random.uniform(0, 1.5 * (attempt + 1))
                        time.sleep(sleep_for)
                        outcome = _read_tile_once((idx, ds))
                        if outcome[0] == "ok":
                            results[idx] = outcome[1:]
                            watchdog.end_ok(idx)
                            recovered += 1
                            success = True
                            logger.info(
                                f"STAC Bild: tile {idx + 1} recovered "
                                f"(serial attempt {attempt + 1})"
                            )
                            break
                        elif outcome[0] == "skip":
                            # Shouldn't normally happen on retry (overlap
                            # is deterministic) but handle it cleanly.
                            watchdog.end_ok(idx)
                            success = True
                            break
                        else:
                            watchdog.end_attempt_fail(idx)
                    if not success:
                        watchdog.end_perm_fail(idx)
                        permanent_failures += 1
                        logger.warning(
                            f"STAC Bild: tile {idx + 1} permanently failed "
                            f"after {max_serial_retries} serial retries"
                        )

                phase2_elapsed = time.monotonic() - phase2_start
                logger.info(
                    f"STAC Bild: phase 2 done in {phase2_elapsed:.1f}s — "
                    f"{recovered} recovered, {permanent_failures} permanent fail"
                )
                if job:
                    job.add_log(
                        f"Phase 2 complete in {phase2_elapsed:.1f}s — "
                        f"{recovered} recovered, {permanent_failures} permanent"
                    )
            else:
                # No failures in Phase 1 (or already timed out).
                # `permanent_failures` already initialised to 0 above.
                pass

            if watchdog.timed_out:
                logger.error(
                    "STAC Bild: aborting merge — watchdog wall-clock cap reached"
                )
                return None, None

            # ----- First-wins composite in newest-first input order.
            # A pixel is filled only if every band is still 0 (nodata).
            # Matches rasterio.merge's method='first' + nodata=0 semantics.
            for r in results:
                if r is None:
                    continue
                _, rs, re_, cs, ce, buf, _ = r
                region = merged[:, rs:re_, cs:ce]
                is_nodata = (region == 0).all(axis=0)
                if not is_nodata.any():
                    continue
                np.copyto(region, buf, where=is_nodata[np.newaxis, :, :])
    except Exception as exc:
        logger.error(f"STAC Bild: merge failed: {exc}")
        return None, None
    finally:
        watchdog.stop()
        # Close VRT wrappers before the DatasetReaders they wrap.
        for ds in datasets:
            if ds in source_handles:
                continue
            try:
                ds.close()
            except Exception:
                pass
        for h in source_handles:
            try:
                h.close()
            except Exception:
                pass

    merge_elapsed = time.monotonic() - merge_start
    merged_mb = merged.nbytes / (1024 * 1024)
    fail_note = (
        f" — {permanent_failures} tile(s) permanently failed (within tolerance)"
        if permanent_failures else ""
    )
    logger.info(
        f"STAC Bild: range-merge complete in {merge_elapsed:.1f}s "
        f"({merged.shape[2]}×{merged.shape[1]} px, {merged_mb:.0f} MB in-memory)"
        f"{fail_note}"
    )
    if job:
        job.add_log(
            f"COG range-merge complete in {merge_elapsed:.1f}s "
            f"({merged.shape[2]}×{merged.shape[1]} px, {merged_mb:.0f} MB)"
            f"{fail_note}"
        )

    # Hard fail if upstream coverage was so degraded that the merged
    # mosaic would have visible holes. A few permanent failures are OK
    # because newer-first overlap usually fills them in, but if more
    # than a third of tiles are lost the user should see a clear error
    # rather than a quietly-degraded orthophoto.
    if permanent_failures and permanent_failures > max(1, len(datasets) // 3):
        logger.error(
            f"STAC Bild: too many tile read failures "
            f"({permanent_failures}/{len(datasets)}) — orthophoto coverage "
            f"would be unacceptably degraded; aborting STAC Bild"
        )
        return None, None

    return merged, out_transform


async def fetch_stac_orthophoto(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    job=None,
) -> Optional[bytes]:
    """
    Fetch the most recent orthophoto for a bbox from Lantmäteriet STAC Bild.

    Drop-in replacement for fetch_historical_orthophoto() — same signature,
    same PNG output format. Returns None on any failure so the caller can
    fall back to the WMS service or Sentinel-2.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84 degrees.
        width: Output image width in pixels.
        height: Output image height in pixels.
        job: Optional MapGenerationJob for progress logging.

    Returns:
        PNG image bytes, or None on failure.
    """
    if not LANTMATERIET_CONFIG.has_credentials():
        logger.info(
            "No Lantmäteriet credentials configured — skipping STAC Bild orthophoto"
        )
        return None

    w, s, e, n = bbox_wgs84
    search_url = f"{LANTMATERIET_CONFIG.stac_bild_endpoint}search"
    query = {
        "bbox": [w, s, e, n],
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],  # newest imagery first
        "limit": 50,
    }

    # ------------------------------------------------------------------ #
    # 1. Search for tiles covering our bbox (open endpoint, no auth)
    # ------------------------------------------------------------------ #
    try:
        if job:
            job.add_log("Searching Lantmäteriet STAC Bild for recent orthophotos...")

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                search_url, json=query, headers=_SEARCH_HEADERS
            )
            resp.raise_for_status()
            features = resp.json().get("features", [])

    except Exception as exc:
        logger.warning(f"STAC Bild search failed: {exc}")
        return None

    if not features:
        logger.info(
            f"STAC Bild: no orthophoto tiles found for bbox "
            f"[{w:.3f},{s:.3f},{e:.3f},{n:.3f}]"
        )
        return None

    logger.info(f"STAC Bild: search returned {len(features)} item(s)")

    # Items are already sorted newest-first. Find the most recent year
    # present in the results so we can report it in the log.
    newest_datetime = features[0].get("properties", {}).get("datetime", "")
    newest_year = newest_datetime[:4] if newest_datetime else "unknown"

    if job:
        job.add_log(
            f"Found {len(features)} orthophoto tile(s) "
            f"(most recent: {newest_year}), reading via COG range requests..."
        )

    # ------------------------------------------------------------------ #
    # 2. Build /vsicurl/ paths for each item's "data" asset
    #    Items are sorted newest-first; rasterio.merge uses 'first' method
    #    by default, so newer tiles take priority over older ones where they
    #    overlap.
    # ------------------------------------------------------------------ #
    vsicurl_hrefs: list[str] = []
    for feat in features:
        href = feat.get("assets", {}).get("data", {}).get("href", "")
        if href.startswith("https://"):
            vsicurl_hrefs.append(f"/vsicurl/{href}")
        else:
            logger.debug(f"Skipping item with unexpected href: {href!r}")

    if not vsicurl_hrefs:
        logger.warning("STAC Bild: no valid data asset HREFs in search results")
        return None

    # ------------------------------------------------------------------ #
    # 3. Convert WGS84 bbox to EPSG:3006 for the COG bounds query.
    #
    # A WGS84 lat/lon rectangle is *not* an axis-aligned rectangle in
    # EPSG:3006 — its four corners project to a trapezoid because the
    # meridians converge. Transforming only the SW and NE corners (the
    # pre-v1.3.4 behaviour) loses the wings of the trapezoid: tiles that
    # actually cover the eastern and western edges of the WGS84 area fall
    # outside the merge bounds and the resulting orthophoto has missing
    # strips near those edges. We project all four corners and take the
    # axis-aligned envelope so every WGS84 corner is inside the merge bbox.
    # ------------------------------------------------------------------ #
    try:
        x_min, y_min, x_max, y_max = _wgs84_bbox_to_epsg3006_envelope((w, s, e, n))
    except Exception as exc:
        logger.error(f"STAC Bild: EPSG:4326 → EPSG:3006 transform failed: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # 4. COG windowed merge (synchronous; rasterio issues HTTP range reqs)
    #
    # Run on a worker thread via asyncio.to_thread so the event loop stays
    # responsive — otherwise the 30-300s blocking merge freezes the FastAPI
    # /job/{id} polling endpoint and the web UI activity log can't update
    # even though job.add_log entries are being appended in the background.
    # ------------------------------------------------------------------ #
    merged_rgb, src_transform = await asyncio.to_thread(
        _cog_merge_rgb,
        vsicurl_hrefs,
        epsg3006_bounds=(x_min, y_min, x_max, y_max),
        target_width=width,
        target_height=height,
        job=job,
    )

    if merged_rgb is None:
        logger.warning("STAC Bild: COG merge returned no data")
        return None

    logger.info(
        f"STAC Bild: COG merge produced {merged_rgb.shape[2]}×{merged_rgb.shape[1]} px "
        f"RGB array (EPSG:3006)"
    )

    # ------------------------------------------------------------------ #
    # 5. Warp from EPSG:3006 → WGS84 at the requested pixel dimensions
    #    This keeps the output in the same format as the WMS service so the
    #    step-7b reprojection pipeline in map_generator.py is unchanged.
    # ------------------------------------------------------------------ #
    try:
        import time as _time

        from rasterio.crs import CRS
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds
        from rasterio.warp import reproject as warp_reproject

        src_crs = CRS.from_epsg(3006)
        dst_crs = CRS.from_epsg(4326)
        dst_transform = from_bounds(w, s, e, n, width, height)
        dst_array = np.zeros((3, height, width), dtype=np.uint8)

        warp_workers = _default_workers()
        warp_start = _time.monotonic()
        logger.info(
            f"STAC Bild: warping EPSG:3006 → WGS84 (Lanczos), "
            f"target {width}×{height} px, 3 bands, {warp_workers} GDAL threads..."
        )
        if job:
            job.add_log(
                f"Warping orthophoto EPSG:3006 → WGS84 at {width}×{height} px "
                f"(Lanczos, 3 bands, {warp_workers} threads)..."
            )

        # Single multi-band reproject — GDAL parallelizes internally via
        # num_threads. Replaces the previous 3-iteration loop (one band at a
        # time on serially-awaited worker threads, ~9s/band).
        await asyncio.to_thread(
            warp_reproject,
            source=merged_rgb,
            destination=dst_array,
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.lanczos,
            num_threads=warp_workers,
        )

        warp_elapsed = _time.monotonic() - warp_start
        logger.info(
            f"STAC Bild: warped 3 bands in {warp_elapsed:.1f}s "
            f"({warp_workers} GDAL threads)"
        )
        if job:
            job.add_log(
                f"Warped 3 orthophoto bands EPSG:3006 → WGS84 in "
                f"{warp_elapsed:.1f}s ({warp_workers} threads)"
            )

    except Exception as exc:
        logger.error(f"STAC Bild: EPSG:3006 → WGS84 warp failed: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # 6. Encode as PNG and return
    # ------------------------------------------------------------------ #
    try:
        import time as _time

        from PIL import Image

        enc_start = _time.monotonic()
        if job:
            job.add_log(
                f"Encoding orthophoto to PNG ({width}×{height} px)..."
            )
        def _encode_png() -> bytes:
            img = Image.fromarray(dst_array.transpose(1, 2, 0))  # (H, W, 3)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        png_bytes = await asyncio.to_thread(_encode_png)
        enc_elapsed = _time.monotonic() - enc_start

        logger.info(
            f"STAC Bild: orthophoto ready — {width}×{height} px, "
            f"{len(png_bytes) / (1024 * 1024):.1f} MB, "
            f"imagery year: {newest_year}, "
            f"PNG-encode {enc_elapsed:.1f}s"
        )
        if job:
            job.add_log(
                f"Downloaded Lantmäteriet orthophoto ({newest_year} imagery, "
                f"{width}×{height} px)",
                "success",
            )
        return png_bytes

    except Exception as exc:
        logger.error(f"STAC Bild: PNG encoding failed: {exc}")
        return None
