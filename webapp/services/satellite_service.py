"""
Satellite imagery and land-cover data service.

Fetches:
- Sentinel-2 Cloudless imagery (EOX WMS) — global, 10 m
- Lantmäteriet Historical Orthophotos (WMS) — Sweden only, 2005 color imagery
- CORINE Land Cover (EEA Discomap WMS)
- Tree Cover Density (Copernicus HRL ImageServer)

Country-aware dispatch: Swedish maps try Lantmäteriet Historical Orthophotos
first with Sentinel-2 as fallback. All other countries use Sentinel-2 directly.
Note: Lantmäteriet orthophotos are from 2005 — areas may look dated.
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

    For Swedish maps, tries Lantmäteriet Historical Orthophotos first
    (2005 color imagery), falls back to Sentinel-2 Cloudless (2021, 10 m)
    for other countries or on failure.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84
        width: Image width in pixels
        height: Image height in pixels
        country_codes: List of detected country codes (e.g. ["SE"])

    Returns:
        Tuple of (image_bytes_or_None, source_name_string)
    """
    if country_codes and "SE" in country_codes:
        try:
            from services.lantmateriet.orthophoto_service import (
                fetch_historical_orthophoto,
            )

            logger.info("Attempting Lantmäteriet Historical Orthophotos (2005 color)...")
            if job:
                job.add_log("Trying Lantmäteriet historical orthophotos...")
            lm_img = await fetch_historical_orthophoto(bbox_wgs84, width, height)
            if lm_img:
                logger.info(
                    f"Using Lantmäteriet orthophoto: {len(lm_img)} bytes"
                )
                return lm_img, "Lantmäteriet Historical Orthophotos (2005)"
            logger.warning(
                "Lantmäteriet orthophoto unavailable, falling back to Sentinel-2"
            )
            if job:
                job.add_log("Lantmäteriet orthophotos not available, falling back to Sentinel-2...", "warning")
        except Exception as e:
            logger.error(f"Error fetching Lantmäteriet orthophoto: {e}")
            logger.warning("Falling back to Sentinel-2 Cloudless")
            if job:
                job.add_log("Lantmäteriet orthophoto error, falling back to Sentinel-2...", "warning")

    if job:
        job.add_log(f"Downloading Sentinel-2 Cloudless imagery ({width}×{height})...")
    data = await fetch_sentinel2_cloudless(bbox_wgs84, width, height)
    return data, "Sentinel-2 Cloudless (EOX)"
