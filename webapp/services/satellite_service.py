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

import httpx

from config import SENTINEL2_WMS_ENDPOINT, CORINE_WMS, TREE_COVER_REST

logger = logging.getLogger(__name__)

# Retry configuration for WMS/satellite services
MAX_WMS_RETRIES = 3
WMS_RETRY_WAIT_S = 5.0
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)


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


async def fetch_sentinel2_cloudless(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
) -> bytes | None:
    """
    Fetch Sentinel-2 Cloudless imagery from EOX WMS.

    Args:
        bbox_wgs84: (west, south, east, north)
        width: Image width in pixels
        height: Image height in pixels

    Returns:
        PNG image bytes or None on failure.
    """
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

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _wms_request_with_retry(client, SENTINEL2_WMS_ENDPOINT, params)
            content_type = resp.headers.get("content-type", "")
            if "image" in content_type:
                logger.info(f"Received {len(resp.content)} bytes of Sentinel-2 imagery")
                return resp.content
            else:
                logger.warning(f"Unexpected content type from EOX: {content_type}")
                return None
    except Exception as e:
        logger.error(f"Failed to fetch Sentinel-2 imagery: {e}")
        return None


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
# Satellite reprojection (WGS84 → terrain CRS)
# ---------------------------------------------------------------------------


def reproject_satellite_to_terrain_crs(
    satellite_path,
    src_bbox: tuple[float, float, float, float],
    dst_crs: str,
    dst_bounds: tuple[float, float, float, float],
    target_size: int,
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
        target_size: Output pixel dimensions (target_size × target_size).

    Returns:
        True on success, False on failure (original file left unchanged on error).
    """
    try:
        from pathlib import Path

        import numpy as np
        from PIL import Image
        from rasterio.crs import CRS
        from rasterio.transform import from_bounds
        from rasterio.warp import Resampling, reproject

        satellite_path = Path(satellite_path)

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
        dst_transform = from_bounds(min_x, min_y, max_x, max_y, target_size, target_size)

        # Allocate destination
        dst_raster = np.zeros((3, target_size, target_size), dtype=np.uint8)

        # Reproject all three bands
        for band in range(3):
            reproject(
                source=src_raster[band],
                destination=dst_raster[band],
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs_obj,
                resampling=Resampling.bilinear,
            )

        # Save reprojected image back to the same path
        result_img = Image.fromarray(dst_raster.transpose(1, 2, 0))
        result_img.save(str(satellite_path), format="PNG")

        logger.info(
            f"Reprojected satellite image EPSG:4326 → {dst_crs} "
            f"({target_size}×{target_size}px, "
            f"bounds: {min_x:.0f},{min_y:.0f} → {max_x:.0f},{max_y:.0f})"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to reproject satellite image to {dst_crs}: {e}")
        return False
