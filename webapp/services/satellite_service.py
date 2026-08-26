"""
Satellite imagery and land-cover data service.

Fetches:
- Sentinel-2 Cloudless imagery (EOX WMS) — global, 10 m
- Lantmäteriet STAC Bild (COG orthophotos) — Sweden only, 2007–2025, 0.16 m/px
- Lantmäteriet Historical Orthophotos (WMS) — Sweden only, 2005 color (fallback)
- CORINE Land Cover (EEA Discomap WMS)
- Tree Cover Density (Copernicus HRL ImageServer)

Country-aware dispatch: Swedish maps try Lantmäteriet STAC Bild first (most
recent imagery, 2007–2025), then fall back to the WMS 2005 layer, then
Sentinel-2. All other countries use Sentinel-2 directly.
"""

from __future__ import annotations

import asyncio
import logging
import math

import httpx

from config import SENTINEL2_WMS_ENDPOINT, CORINE_WMS, TREE_COVER_REST

logger = logging.getLogger(__name__)

# Retry configuration for WMS/satellite services
MAX_WMS_RETRIES = 3
WMS_RETRY_WAIT_S = 5.0
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)

# EOX's WMS rejects any GetMap larger than this per axis with HTTP 400. The
# limit is undocumented - it does not appear as MaxWidth/MaxHeight in their
# GetCapabilities - and was found by bisection: 4096 succeeds, 4097 does not.
#
# Since v1.3.6 the satellite target has been 4x the heightmap capped at 8192, so
# every non-Swedish map above ~2 km asked for more than 4096 and got nothing
# back at all (issue #97). Swedish maps were unaffected because the Lantmateriet
# STAC path reads tiled COGs and has no such limit.
#
# The fix is to tile the request, NOT to clamp it: clamping would shrink the
# satellite for every non-Swedish user, and the output resolution is something
# users have specifically asked us to preserve.
EOX_WMS_MAX_DIM = 4096

# How many tiles to request at once. EOX takes ~14 s for a full 4096 tile, so
# some concurrency is needed, but a 3x3 fetch firing nine simultaneous
# multi-megapixel renders is a good way to get rate-limited.
EOX_WMS_TILE_CONCURRENCY = 4


async def _wms_request_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict,
    max_retries: int = MAX_WMS_RETRIES,
) -> httpx.Response:
    """
    Execute a WMS request with retry logic for transient errors.

    Retries on 429, 502, 503, 504 status codes with exponential backoff.

    Args:
        client: httpx AsyncClient instance
        endpoint: WMS endpoint URL
        params: Request parameters
        max_retries: Maximum number of retry attempts

    Returns:
        httpx.Response on success

    Raises:
        httpx.HTTPStatusError: On non-retryable HTTP errors or after exhausting retries
    """
    last_exception = None

    for attempt in range(max_retries):
        try:
            resp = await client.get(endpoint, params=params)

            # Success - return immediately
            if resp.status_code == 200:
                return resp

            # Check if status code is retryable
            if resp.status_code in RETRYABLE_STATUS_CODES:
                logger.warning(
                    f"WMS request returned retryable status {resp.status_code} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

                # Don't wait after the last attempt
                if attempt < max_retries - 1:
                    wait_time = WMS_RETRY_WAIT_S * (2 ** attempt)  # Exponential backoff
                    logger.info(f"Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

            # Non-retryable status code - raise immediately
            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            last_exception = e
            # If it's a retryable status code and we have retries left, continue
            if e.response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                wait_time = WMS_RETRY_WAIT_S * (2 ** attempt)
                logger.warning(
                    f"WMS request failed with status {e.response.status_code}, "
                    f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)
                continue
            # Otherwise, raise the exception
            raise
        except Exception as e:
            last_exception = e
            logger.error(f"WMS request failed with exception: {e}")
            raise

    # If we get here, all retries were exhausted
    if last_exception:
        raise last_exception

    # This shouldn't happen, but just in case
    raise httpx.HTTPStatusError(
        f"WMS request failed after {max_retries} retries",
        request=resp.request,
        response=resp,
    )


def split_pixel_axis(total_px: int, max_px: int) -> list[tuple[int, int]]:
    """
    Split ``total_px`` into contiguous ``(start, end)`` ranges of at most
    ``max_px``, as evenly as possible.

    The ranges tile the axis exactly - no gaps, no overlap, and the last range
    ends at ``total_px`` - so a stitched mosaic is exactly the requested size.
    """
    if total_px <= max_px:
        return [(0, total_px)]
    n = math.ceil(total_px / max_px)
    edges = [round(i * total_px / n) for i in range(n + 1)]
    return [(edges[i], edges[i + 1]) for i in range(n)]


def _sub_bbox(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
) -> tuple[float, float, float, float]:
    """
    Map a pixel window back to its WGS84 sub-bbox.

    A WMS GetMap in EPSG:4326 is a linear mapping from the bbox to the pixel
    grid, so a pixel sub-rectangle corresponds to a proportional lon/lat
    sub-rectangle. Row 0 is the *north* edge, hence the inverted y mapping.

    Each tile's bbox aspect therefore matches its pixel aspect automatically -
    which matters, because EOX returns HTTP 400 when the two disagree.
    """
    west, south, east, north = bbox_wgs84
    lon_span = east - west
    lat_span = north - south
    x0, x1 = x_range
    y0, y1 = y_range
    return (
        west + lon_span * (x0 / width),
        north - lat_span * (y1 / height),
        west + lon_span * (x1 / width),
        north - lat_span * (y0 / height),
    )


async def _fetch_sentinel2_window(
    client: httpx.AsyncClient,
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
) -> bytes | None:
    """Fetch a single GetMap window (must already be within EOX_WMS_MAX_DIM)."""
    w, s, e, n = bbox_wgs84
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": "s2cloudless-2021",
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": f"{w},{s},{e},{n}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/png",
    }
    resp = await _wms_request_with_retry(client, SENTINEL2_WMS_ENDPOINT, params)
    content_type = resp.headers.get("content-type", "")
    if "image" not in content_type:
        logger.warning(f"Unexpected content type from EOX: {content_type}")
        return None
    return resp.content


def _stitch_tiles(
    tiles: dict[tuple[int, int], bytes],
    x_ranges: list[tuple[int, int]],
    y_ranges: list[tuple[int, int]],
    width: int,
    height: int,
) -> bytes:
    """Paste the fetched tiles into one PNG of exactly ``width`` x ``height``."""
    import io

    from PIL import Image

    canvas = Image.new("RGB", (width, height))
    for (col, row), data in tiles.items():
        x0, x1 = x_ranges[col]
        y0, y1 = y_ranges[row]
        with Image.open(io.BytesIO(data)) as tile:
            # EOX answers image/jpeg even when FORMAT=image/png is requested,
            # so normalise rather than assuming the mode.
            rgb = tile.convert("RGB")
        if rgb.size != (x1 - x0, y1 - y0):
            # A server that ignored our WIDTH/HEIGHT would silently shift every
            # later tile; resample so the mosaic stays aligned.
            logger.warning(
                f"EOX tile ({col},{row}) came back {rgb.size}, expected "
                f"{(x1 - x0, y1 - y0)} - resampling to keep the mosaic aligned"
            )
            rgb = rgb.resize((x1 - x0, y1 - y0), Image.LANCZOS)
        canvas.paste(rgb, (x0, y0))

    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


async def fetch_sentinel2_cloudless(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
) -> bytes | None:
    """
    Fetch Sentinel-2 Cloudless imagery from EOX WMS.

    Requests larger than ``EOX_WMS_MAX_DIM`` on either axis are split into a
    grid of tiles and stitched back together, because EOX rejects anything
    bigger with HTTP 400 (issue #97). The returned image is always the full
    requested size - tiling is an implementation detail, not a downgrade.

    Args:
        bbox_wgs84: (west, south, east, north)
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        PNG image bytes or None on failure.
    """
    x_ranges = split_pixel_axis(width, EOX_WMS_MAX_DIM)
    y_ranges = split_pixel_axis(height, EOX_WMS_MAX_DIM)

    # Fast path: small enough for a single request, exactly as before.
    if len(x_ranges) == 1 and len(y_ranges) == 1:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                data = await _fetch_sentinel2_window(client, bbox_wgs84, width, height)
            if data:
                logger.info(f"Received {len(data)} bytes of Sentinel-2 imagery")
            return data
        except Exception as e:
            logger.error(f"Failed to fetch Sentinel-2 imagery: {e}")
            return None

    jobs = [
        (col, row, x_ranges[col], y_ranges[row])
        for row in range(len(y_ranges))
        for col in range(len(x_ranges))
    ]
    logger.info(
        f"Sentinel-2 request is {width}x{height}, above EOX's {EOX_WMS_MAX_DIM} px "
        f"limit - fetching as {len(x_ranges)}x{len(y_ranges)} tiles "
        f"({len(jobs)} requests)"
    )

    tiles: dict[tuple[int, int], bytes] = {}
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            # Phase 1: bounded-concurrency sweep. _wms_request_with_retry
            # already handles retryable statuses within each request.
            sem = asyncio.Semaphore(EOX_WMS_TILE_CONCURRENCY)

            async def _one(col, row, xr, yr):
                async with sem:
                    try:
                        return (col, row), await _fetch_sentinel2_window(
                            client,
                            _sub_bbox(bbox_wgs84, width, height, xr, yr),
                            xr[1] - xr[0],
                            yr[1] - yr[0],
                        )
                    except Exception as e:  # noqa: BLE001 - retried in phase 2
                        logger.warning(f"Sentinel-2 tile ({col},{row}) failed: {e}")
                        return (col, row), None

            for key, data in await asyncio.gather(
                *(_one(c, r, xr, yr) for c, r, xr, yr in jobs)
            ):
                if data:
                    tiles[key] = data

            # Phase 2: serial sweep for whatever phase 1 missed. A flaky
            # upstream recovers far better when it is not being hammered in
            # parallel (same pattern as the STAC reader).
            missing = [j for j in jobs if (j[0], j[1]) not in tiles]
            if missing:
                logger.warning(
                    f"{len(missing)} Sentinel-2 tile(s) failed the parallel sweep - "
                    f"retrying them serially"
                )
                for col, row, xr, yr in missing:
                    try:
                        data = await _fetch_sentinel2_window(
                            client,
                            _sub_bbox(bbox_wgs84, width, height, xr, yr),
                            xr[1] - xr[0],
                            yr[1] - yr[0],
                        )
                        if data:
                            tiles[(col, row)] = data
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            f"Sentinel-2 tile ({col},{row}) failed the serial "
                            f"retry too: {e}"
                        )
    except Exception as e:
        logger.error(f"Failed to fetch Sentinel-2 imagery: {e}")
        return None

    if len(tiles) != len(jobs):
        # A hole in the mosaic would import as a black rectangle in the middle
        # of the terrain. Fail loudly and let the caller continue without a
        # satellite rather than ship a visibly broken texture.
        logger.error(
            f"Sentinel-2 tiling incomplete: {len(tiles)}/{len(jobs)} tiles - "
            f"discarding the mosaic rather than shipping one with holes"
        )
        return None

    try:
        stitched = _stitch_tiles(tiles, x_ranges, y_ranges, width, height)
    except Exception as e:
        logger.error(f"Failed to stitch Sentinel-2 tiles: {e}")
        return None

    logger.info(
        f"Received {len(stitched)} bytes of Sentinel-2 imagery "
        f"({len(jobs)} tiles stitched to {width}x{height})"
    )
    return stitched


async def fetch_copernicus_landcover(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
) -> bytes | None:
    """
    Fetch CORINE Land Cover from EEA Discomap WMS.

    Returns PNG image bytes with land cover classes encoded as colours.
    """
    w, s, e, n = bbox_wgs84
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.3.0",
        "REQUEST": "GetMap",
        "LAYERS": "12",
        "CRS": "EPSG:4326",
        "BBOX": f"{s},{w},{n},{e}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/png",
        "STYLES": "",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _wms_request_with_retry(client, CORINE_WMS, params)
            content_type = resp.headers.get("content-type", "")
            if "image" in content_type:
                logger.info(f"Received {len(resp.content)} bytes of CORINE data")
                return resp.content
            return None
    except Exception as e:
        logger.error(f"Failed to fetch CORINE land cover: {e}")
        return None


async def fetch_tree_cover_density(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
) -> bytes | None:
    """
    Fetch Tree Cover Density from Copernicus HRL via ArcGIS ImageServer.

    Returns TIFF bytes with density values 0-100.
    """
    w, s, e, n = bbox_wgs84
    url = TREE_COVER_REST + "/exportImage"
    params = {
        "bbox": f"{w},{s},{e},{n}",
        "bboxSR": "4326",
        "size": f"{width},{height}",
        "format": "tiff",
        "f": "image",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _wms_request_with_retry(client, url, params)
            logger.info(f"Received {len(resp.content)} bytes of tree cover data")
            return resp.content
    except Exception as e:
        logger.error(f"Failed to fetch tree cover density: {e}")
        return None


# ---------------------------------------------------------------------------
# Country-aware satellite imagery dispatcher
# ---------------------------------------------------------------------------


async def fetch_satellite_imagery(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    country_codes: list[str] | None = None,
    job=None,
) -> tuple[bytes | None, str]:
    """
    Fetch satellite imagery with country-based source priority.

    For Swedish maps, tries Lantmäteriet STAC Bild first (2007–2025 COG
    orthophotos at 0.16 m/px via HTTP range requests), then falls back to the
    legacy WMS 2005 colour layer, and finally to Sentinel-2 Cloudless (10 m).
    All other countries use Sentinel-2 directly.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84
        width: Image width in pixels
        height: Image height in pixels
        country_codes: List of detected country codes (e.g. ["SE"])
        job: Optional MapGenerationJob for progress logging

    Returns:
        Tuple of (image_bytes_or_None, source_name_string)
    """
    if country_codes and "SE" in country_codes:
        # ------------------------------------------------------------------ #
        # 1. Try STAC Bild first — most recent orthophotos (2007–2025, 0.16 m)
        # ------------------------------------------------------------------ #
        try:
            from config.lantmateriet import LANTMATERIET_CONFIG
            from services.lantmateriet.stac_orthophoto_service import (
                fetch_stac_orthophoto,
            )

            if LANTMATERIET_CONFIG.has_credentials():
                logger.info("Attempting Lantmäteriet STAC Bild (2007–2025 orthophotos)...")
                stac_img = await fetch_stac_orthophoto(bbox_wgs84, width, height, job)
                if stac_img:
                    logger.info(
                        f"Using Lantmäteriet STAC Bild orthophoto: {len(stac_img)} bytes"
                    )
                    return stac_img, "Lantmäteriet STAC Bild (most recent orthophoto)"
                logger.warning(
                    "STAC Bild orthophoto unavailable, falling back to WMS 2005"
                )
                if job:
                    job.add_log("STAC Bild not available, trying historical orthophotos...", "warning")
            else:
                logger.info("No Lantmäteriet credentials — skipping STAC Bild")
        except Exception as e:
            logger.error(f"Error fetching Lantmäteriet STAC Bild orthophoto: {e}")
            logger.warning("Falling back to WMS 2005 orthophoto")

        # ------------------------------------------------------------------ #
        # 2. Fall back to WMS historical orthophotos (2005 colour layer)
        # ------------------------------------------------------------------ #
        try:
            from services.lantmateriet.orthophoto_service import (
                fetch_historical_orthophoto,
            )

            logger.info("Attempting Lantmäteriet Historical Orthophotos (2005 color)...")
            if job:
                job.add_log("Trying Lantmäteriet historical orthophotos (2005)...")
            lm_img = await fetch_historical_orthophoto(bbox_wgs84, width, height)
            if lm_img:
                logger.info(
                    f"Using Lantmäteriet WMS orthophoto: {len(lm_img)} bytes"
                )
                return lm_img, "Lantmäteriet Historical Orthophotos (2005)"
            logger.warning(
                "Lantmäteriet WMS orthophoto unavailable, falling back to Sentinel-2"
            )
            if job:
                job.add_log("Lantmäteriet orthophotos not available, falling back to Sentinel-2...", "warning")
        except Exception as e:
            logger.error(f"Error fetching Lantmäteriet WMS orthophoto: {e}")
            logger.warning("Falling back to Sentinel-2 Cloudless")
            if job:
                job.add_log("Lantmäteriet orthophoto error, falling back to Sentinel-2...", "warning")

    if job:
        job.add_log(f"Downloading Sentinel-2 Cloudless imagery ({width}×{height})...")
    data = await fetch_sentinel2_cloudless(bbox_wgs84, width, height)
    return data, "Sentinel-2 Cloudless (EOX)"


# ---------------------------------------------------------------------------
# Satellite texture dimensions
# ---------------------------------------------------------------------------

# How many times larger the satellite texture is than the heightmap, per axis.
# The diffuse texture is imported via the Terrain Tool > Import Satellite Map
# step, independent of the heightmap vertex grid, so it can carry far more
# detail. With high-resolution sources (e.g. Lantmäteriet STAC Bild at
# 0.16 m/px) the previous heightmap-matched dimensions threw away ~15× the
# source detail (#67).
SATELLITE_RESOLUTION_MULTIPLIER = 4

# Hard cap on satellite texture dimensions. 8192 is a safe modern texture
# size; larger values risk Workbench import problems and big RAM/disk costs.
SATELLITE_MAX_DIM = 8192

# Hard cap on the *fetch* dimensions, which are deliberately allowed to exceed
# SATELLITE_MAX_DIM. When the terrain uses a projected CRS the WGS84 fetch box
# is the envelope of the projected rectangle, so it covers more ground than the
# terrain does. Fetching that envelope at SATELLITE_MAX_DIM leaves the terrain
# occupying only 1/ratio of those pixels, and the reprojection upsamples the
# difference back out. Fetching at target x ratio keeps the terrain at full
# target density instead.
#
# The ratio is small because SWEREF/UTM are conformal and near north-up over a
# 5-10 km box: measured 1.006 at Froson, 1.026 in Skane, rising to 1.139 in the
# far north-east of Sweden (68N 23E) where grid convergence is largest. So this
# is worth ~0.5-3% of linear detail on a typical map and ~14% at the extreme -
# a real fix, not a dramatic one.
#
# The output PNG is still capped at SATELLITE_MAX_DIM; this governs only the
# intermediate fetch. 16384 bounds the worst case at ~800 MB of RGB working
# set, which the tiled STAC reader handles comfortably.
SATELLITE_MAX_FETCH_DIM = 16384


def compute_satellite_target_dims(
    heightmap_x: int, heightmap_z: int,
) -> tuple[int, int]:
    """
    Compute the target satellite texture dimensions given the heightmap size.

    Multiplies each axis by ``SATELLITE_RESOLUTION_MULTIPLIER`` and caps at
    ``SATELLITE_MAX_DIM`` so we don't exceed the engine's texture limit or
    blow up the export size.
    """
    # Never let the satellite be smaller than the heightmap, even at the
    # 8193 vertex max where multiplier × dim exceeds the cap by a lot.
    sat_x = min(SATELLITE_MAX_DIM, heightmap_x * SATELLITE_RESOLUTION_MULTIPLIER)
    sat_z = min(SATELLITE_MAX_DIM, heightmap_z * SATELLITE_RESOLUTION_MULTIPLIER)
    sat_x = max(sat_x, heightmap_x)
    sat_z = max(sat_z, heightmap_z)
    return int(sat_x), int(sat_z)


def compute_satellite_fetch_dims(
    target_x: int, target_z: int, ratio_x: float, ratio_y: float,
) -> tuple[int, int]:
    """
    Compute the WGS84 fetch dimensions for a projected-CRS terrain.

    ``ratio_*`` is how much wider/taller the WGS84 envelope of the projected
    rectangle is than the terrain's own WGS84 bbox (always >= 1.0). The fetch
    has to cover that whole envelope, so it needs ``target x ratio`` pixels to
    leave the terrain itself at ``target`` density once the reprojection crops
    back down.

    Capped at ``SATELLITE_MAX_FETCH_DIM``, deliberately above
    ``SATELLITE_MAX_DIM`` - this sizes the intermediate fetch, not the output
    PNG, which ``compute_satellite_target_dims`` still caps at
    ``SATELLITE_MAX_DIM``.
    """
    fetch_x = min(SATELLITE_MAX_FETCH_DIM, int(math.ceil(target_x * max(ratio_x, 1.0))))
    fetch_z = min(SATELLITE_MAX_FETCH_DIM, int(math.ceil(target_z * max(ratio_y, 1.0))))
    # Never fetch below the target: that would upsample, which is the whole
    # defect this function exists to avoid.
    return int(max(fetch_x, target_x)), int(max(fetch_z, target_z))


# ---------------------------------------------------------------------------
# Satellite reprojection (WGS84 → terrain CRS)
# ---------------------------------------------------------------------------


def reproject_satellite_to_terrain_crs(
    satellite_path,
    src_bbox: tuple[float, float, float, float],
    dst_crs: str,
    dst_bounds: tuple[float, float, float, float],
    target_size: tuple[int, int] | int,
    job=None,
) -> bool:
    """
    Reproject satellite_map.png from WGS84 to the terrain's native projected CRS.

    The satellite image is fetched in WGS84 and linearly stretched, while roads
    and the heightmap use a projected CRS (e.g. EPSG:3006 for Sweden). Without
    reprojection the EPSG:3006 grid is rotated ~0.5° relative to WGS84 lat/lon
    lines at high latitudes, causing up to ~90 m of road/satellite misalignment
    across a 5 km terrain.

    This function warps the image so that its pixel grid aligns with the same
    projected bounding box used by the heightmap and road coordinate transformer.
    The file is modified in-place.

    Args:
        satellite_path: Path to the PNG to reproject (modified in-place).
        src_bbox: WGS84 bounding box (west, south, east, north).
        dst_crs: Target CRS string, e.g. "EPSG:3006".
        dst_bounds: Bounding box in dst_crs (min_x, min_y, max_x, max_y).
            These are the _sw_projected and _ne_projected values from
            CoordinateTransformer.
        target_size: Output pixel dimensions as (width, height). For backwards
            compatibility a scalar is also accepted and produces a square output.

    Returns:
        True on success, False on failure (original file left unchanged on error).
    """
    try:
        import os
        import time
        from pathlib import Path

        import numpy as np
        from PIL import Image
        from rasterio.crs import CRS
        from rasterio.transform import from_bounds
        from rasterio.warp import Resampling, reproject

        if isinstance(target_size, int):
            target_w, target_h = target_size, target_size
        else:
            target_w, target_h = int(target_size[0]), int(target_size[1])

        warp_threads = min(8, os.cpu_count() or 2)

        satellite_path = Path(satellite_path)
        if job:
            job.add_log(
                f"Reprojecting satellite WGS84 → {dst_crs} "
                f"at {target_w}×{target_h} px (Lanczos, "
                f"{warp_threads} GDAL threads)..."
            )

        # Load source image
        img = Image.open(satellite_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        src_array = np.array(img)          # (H, W, 3)
        src_h, src_w = src_array.shape[:2]

        # rasterio expects (bands, H, W)
        src_raster = src_array.transpose(2, 0, 1).astype(np.uint8)

        # Source affine: WGS84, north-up (standard rasterio convention)
        west, south, east, north = src_bbox
        src_crs = CRS.from_epsg(4326)
        src_transform = from_bounds(west, south, east, north, src_w, src_h)

        # Destination affine: projected CRS, north-up
        min_x, min_y, max_x, max_y = dst_bounds
        dst_crs_obj = CRS.from_string(dst_crs)
        dst_transform = from_bounds(min_x, min_y, max_x, max_y, target_w, target_h)

        # Allocate destination (rasterio expects bands × H × W)
        dst_raster = np.zeros((3, target_h, target_w), dtype=np.uint8)

        # Single multi-band reproject. The previous implementation looped
        # over bands and paid the warp setup cost three times; collapsing
        # to one call lets GDAL parallelise across bands via num_threads
        # (8 threads cut a 3-band 8192² warp from ~27s to under 10s in
        # production logs — mirror of the v1.5.4 fix in
        # stac_orthophoto_service.py).
        #
        # Lanczos preserves more high-frequency detail than bilinear —
        # important when the source is sub-metre imagery (Lantmäteriet
        # STAC Bild at 0.16 m/px) being warped to a sub-metre output
        # texture (see #67).
        warp_start = time.monotonic()
        reproject(
            source=src_raster,
            destination=dst_raster,
            src_transform=src_transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs_obj,
            resampling=Resampling.lanczos,
            num_threads=warp_threads,
        )
        warp_elapsed = time.monotonic() - warp_start
        logger.info(
            f"Satellite reproject: warped 3 bands in {warp_elapsed:.1f}s "
            f"({warp_threads} GDAL threads)"
        )
        if job:
            job.add_log(
                f"Satellite reprojection complete in {warp_elapsed:.1f}s "
                f"({warp_threads} GDAL threads)"
            )

        # Save reprojected image back to the same path
        result_img = Image.fromarray(dst_raster.transpose(1, 2, 0))
        result_img.save(str(satellite_path), format="PNG")

        logger.info(
            f"Reprojected satellite image EPSG:4326 → {dst_crs} "
            f"({target_w}×{target_h}px, "
            f"bounds: {min_x:.0f},{min_y:.0f} → {max_x:.0f},{max_y:.0f})"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to reproject satellite image to {dst_crs}: {e}")
        return False
