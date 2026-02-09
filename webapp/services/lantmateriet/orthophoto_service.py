"""
Lantmäteriet historical orthophotos (WMS) service.

Provides aerial imagery for Swedish maps from Lantmäteriet's
Historiska Ortofoton WMS. Falls back to Sentinel-2 if unavailable.

Important notes:
- The most recent COLOR layer is from 2005 (OI.Histortho_color_2005).
  This means imagery may look dated for areas that have changed since 2005.
- Resolution varies by year/layer but is generally better than Sentinel-2 (10 m).
- Protocol: WMS 1.1.1.
- Authentication: Basic auth (username/password).
- Available layers include: OI.Histortho_color_2005, OI.Histortho_bw_1960,
  OI.Histortho_bw_1975, etc.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from config.lantmateriet import LANTMATERIET_CONFIG
from services.lantmateriet.auth import get_authenticated_headers

logger = logging.getLogger(__name__)

# Retry configuration (matches satellite_service.py)
MAX_RETRIES = 3
RETRY_WAIT_S = 5.0
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)


async def fetch_historical_orthophoto(
    bbox_wgs84: tuple[float, float, float, float],
    width: int,
    height: int,
    layer: str = "OI.Histortho_color_2005",
    year: Optional[str] = None,
) -> Optional[bytes]:
    """
    Fetch historical orthophoto from Lantmäteriet WMS.

    Drop-in replacement for fetch_sentinel2_cloudless() when used
    for Swedish maps. Returns PNG image bytes.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84
        width: Image width in pixels (max 4096)
        height: Image height in pixels (max 4096)
        layer: WMS layer name (default: "OI.Histortho_color_2005")
        year: Optional year filter for WMS TIME parameter

    Returns:
        PNG bytes or None on failure.
    """
    headers = get_authenticated_headers()
    if "Authorization" not in headers:
        logger.warning("No Lantmäteriet credentials — cannot fetch orthophoto")
        return None

    # Lantmäteriet WMS max dimension is 4096 pixels
    width = min(width, LANTMATERIET_CONFIG.max_tile_size)
    height = min(height, LANTMATERIET_CONFIG.max_tile_size)

    w, s, e, n = bbox_wgs84
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": layer,
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": f"{w},{s},{e},{n}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/png",
    }

    if year:
        params["TIME"] = year  # WMS temporal parameter

    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.get(
                    LANTMATERIET_CONFIG.orthophoto_wms,
                    params=params,
                    headers=headers,
                )

                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    if "image" in content_type:
                        logger.info(
                            f"Received {len(resp.content)} bytes of Lantmäteriet "
                            f"orthophoto ({width}x{height} px)"
                        )
                        return resp.content
                    else:
                        logger.error(
                            f"Unexpected content type from orthophoto WMS: {content_type}"
                        )
                        # Check if it's an XML error
                        if "xml" in content_type.lower():
                            logger.error(f"WMS error response: {resp.text[:500]}")
                        return None

                # Retryable status codes
                if resp.status_code in RETRYABLE_STATUS_CODES:
                    logger.warning(
                        f"Orthophoto WMS returned {resp.status_code} "
                        f"(attempt {attempt + 1}/{MAX_RETRIES})"
                    )
                    if attempt < MAX_RETRIES - 1:
                        wait_time = RETRY_WAIT_S * (2 ** attempt)
                        logger.info(f"Retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue

                # Non-retryable error
                resp.raise_for_status()

        except httpx.HTTPStatusError as exc:
            last_exception = exc
            if (
                exc.response.status_code in RETRYABLE_STATUS_CODES
                and attempt < MAX_RETRIES - 1
            ):
                wait_time = RETRY_WAIT_S * (2 ** attempt)
                logger.warning(
                    f"Orthophoto WMS error {exc.response.status_code}, "
                    f"retrying in {wait_time}s..."
                )
                await asyncio.sleep(wait_time)
                continue
            logger.error(f"Orthophoto WMS HTTP error: {exc.response.status_code}")
            return None
        except Exception as exc:
            logger.error(f"Error fetching historical orthophoto: {exc}")
            return None

    if last_exception:
        logger.error(f"Orthophoto WMS failed after {MAX_RETRIES} retries")
    return None
