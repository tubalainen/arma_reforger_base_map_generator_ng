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
3. Open COGs via rasterio.Env(GDAL_HTTP_AUTH=BASIC, …) + /vsicurl/ prefix
4. rasterio.merge(bounds=projected_bbox, res=target_res, indexes=[1,2,3])
   — reads only RGB bands (drops NIR band 4), uses COG overviews for efficiency
5. Warp merged EPSG:3006 result to WGS84 at the requested pixel dimensions
6. Return PNG bytes — same format as the WMS service, so the existing
   reprojection step (step 7b in map_generator) aligns it correctly
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import httpx
import numpy as np

from config.lantmateriet import LANTMATERIET_CONFIG

logger = logging.getLogger(__name__)

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
    import threading
    import time

    import rasterio
    from rasterio.env import Env
    from rasterio.enums import Resampling
    from rasterio.merge import merge as rasterio_merge

    x_min, y_min, x_max, y_max = epsg3006_bounds
    n_hrefs = len(vsicurl_hrefs)

    datasets = []
    open_start = time.monotonic()
    with Env(**_gdal_vsicurl_env()):
        # Open all COG files (only the header is fetched at this point)
        for idx, href in enumerate(vsicurl_hrefs, 1):
            try:
                ds = rasterio.open(href)
                datasets.append(ds)
                # Header-only open should be quick; log every tile so the user
                # can see the loop progressing (one of ~50 lines per request).
                logger.info(
                    f"STAC Bild: opened COG {idx}/{n_hrefs} "
                    f"({ds.width}×{ds.height} px native)"
                )
            except Exception as exc:
                logger.warning(f"Could not open COG {href}: {exc}")
        open_elapsed = time.monotonic() - open_start
        logger.info(
            f"STAC Bild: opened {len(datasets)}/{n_hrefs} COG headers "
            f"in {open_elapsed:.1f}s"
        )
        if job:
            job.add_log(
                f"Opened {len(datasets)}/{n_hrefs} Lantmäteriet COG tile headers "
                f"in {open_elapsed:.1f}s — starting range-merge over target bounds..."
            )

        if not datasets:
            return None, None

        # Hint rasterio toward a coarse overview level when one would suffice:
        # pick a target resolution that yields roughly `target_width` pixels at
        # the requested bounds, but only as a hint — merge uses the *closest
        # source overview* whose resolution is finer or equal to this value.
        # The merged output keeps that overview's native resolution (no
        # secondary resampling here), so tile windows stay grid-aligned.
        approx_res = max(
            (x_max - x_min) / max(target_width, 1),
            (y_max - y_min) / max(target_height, 1),
        )

        logger.info(
            f"STAC Bild: merging {len(datasets)} tiles at "
            f"approx_res={approx_res:.2f} m/px over bounds "
            f"{int(x_max - x_min)}×{int(y_max - y_min)} m "
            f"→ target {target_width}×{target_height} px"
        )
        if job:
            # rasterio.merge is a single blocking call with no progress hook —
            # warn the user up front so the UI doesn't look frozen.
            job.add_log(
                f"Merging {len(datasets)} orthophoto tiles into final image — "
                f"this can take 30–120 seconds and the activity log will look "
                f"idle while it runs. Please stand by..."
            )

        merge_start = time.monotonic()

        # Heartbeat thread: emit a progress line every 5s so docker logs and the
        # web UI activity log show forward motion during the blocking merge.
        heartbeat_stop = threading.Event()

        def _heartbeat():
            elapsed = 0
            while not heartbeat_stop.wait(5.0):
                elapsed += 5
                logger.info(
                    f"STAC Bild: merge in progress... {elapsed}s elapsed"
                )
                if job:
                    job.add_log(
                        f"...still merging orthophoto tiles ({elapsed}s elapsed)"
                    )

        heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
        heartbeat_thread.start()

        try:
            # indexes=[1, 2, 3] selects the RGB bands (band 4 is NIR — skip it).
            # target_aligned_pixels=True snaps the output extent so pixels are
            # integer multiples of `res` from the origin: every source tile
            # resamples into the *same* shared grid, so adjacent tiles can no
            # longer leave a sub-pixel sliver between them.
            merged, transform = rasterio_merge(
                datasets,
                bounds=(x_min, y_min, x_max, y_max),
                target_aligned_pixels=True,
                res=approx_res,
                resampling=Resampling.bilinear,
                indexes=[1, 2, 3],
                nodata=0,
            )
            # merged shape: (3, H, W) — exact pixel count depends on the
            # overview rasterio picked, the caller warps to the final size.
        except Exception as exc:
            logger.error(f"rasterio.merge failed for STAC Bild COGs: {exc}")
            return None, None
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)
            for ds in datasets:
                try:
                    ds.close()
                except Exception:
                    pass

        merge_elapsed = time.monotonic() - merge_start
        merged_mb = merged.nbytes / (1024 * 1024)
        logger.info(
            f"STAC Bild: range-merge complete in {merge_elapsed:.1f}s "
            f"({merged.shape[2]}×{merged.shape[1]} px, {merged_mb:.0f} MB in-memory)"
        )
        if job:
            job.add_log(
                f"COG range-merge complete in {merge_elapsed:.1f}s "
                f"({merged.shape[2]}×{merged.shape[1]} px, {merged_mb:.0f} MB)"
            )

    return merged, transform


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

        warp_start = _time.monotonic()
        logger.info(
            f"STAC Bild: warping EPSG:3006 → WGS84 (Lanczos), "
            f"target {width}×{height} px, 3 bands..."
        )
        if job:
            job.add_log(
                f"Warping orthophoto EPSG:3006 → WGS84 at {width}×{height} px "
                f"(Lanczos, 3 bands)..."
            )

        # Each band warp is ~9s blocking. Run each on a worker thread so the
        # event loop unblocks between bands and the UI activity log can refresh.
        for band in range(3):
            band_start = _time.monotonic()
            await asyncio.to_thread(
                warp_reproject,
                source=merged_rgb[band],
                destination=dst_array[band],
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.lanczos,
            )
            band_elapsed = _time.monotonic() - band_start
            logger.info(
                f"STAC Bild: warped band {band + 1}/3 in {band_elapsed:.1f}s"
            )
            if job:
                job.add_log(
                    f"Warped orthophoto band {band + 1}/3 in {band_elapsed:.1f}s"
                )

        warp_elapsed = _time.monotonic() - warp_start
        logger.info(f"STAC Bild: warp complete in {warp_elapsed:.1f}s")
        if job:
            job.add_log(f"Orthophoto warp complete in {warp_elapsed:.1f}s")

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
