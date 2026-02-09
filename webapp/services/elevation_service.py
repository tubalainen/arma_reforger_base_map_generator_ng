"""
Elevation data acquisition service.

Fetches DEM data from country-specific WCS services (1.0, 1.1, 2.0)
with OpenTopography global fallback (COP30, SRTM, ALOS).

Includes automatic area chunking for APIs with maximum SUBSET size limits
(e.g. Finland NLS limits elevation requests to 10 000 × 10 000 m).
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
from typing import Optional

import httpx
import numpy as np

from config import (
    ELEVATION_CONFIGS,
    OPENTOPOGRAPHY_ENDPOINT,
    OPENTOPOGRAPHY_API_KEY,
)
from services.utils.geo import transform_bbox_to_crs, estimate_bbox_dimensions_m

logger = logging.getLogger(__name__)


def _sanitize_url(url: str) -> str:
    """
    Sanitize URL by masking API keys and sensitive parameters.

    Replaces values of parameters like api-key, API_Key, token, etc. with '***'.
    """
    import re
    # Mask common API key parameter patterns
    sensitive_params = ['api-key', 'API_Key', 'api_key', 'apikey', 'token', 'access_token', 'key']
    for param in sensitive_params:
        # Match param=value or param%3Dvalue (URL encoded =)
        url = re.sub(
            rf'({param}[=])[^&]+',
            r'\1***',
            url,
            flags=re.IGNORECASE
        )
        url = re.sub(
            rf'({param}%3D)[^&]+',
            r'\1***',
            url,
            flags=re.IGNORECASE
        )
    return url


# ---------------------------------------------------------------------------
# Retry configuration for WCS requests
# ---------------------------------------------------------------------------

# Retryable HTTP status codes: gateway errors and rate limiting
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)
MAX_WCS_RETRIES = 3
WCS_RETRY_WAIT_S = 5.0


async def _wcs_request_with_retry(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict | list,
    max_retries: int = MAX_WCS_RETRIES,
) -> httpx.Response:
    """
    Execute a WCS request with retry logic for transient errors.

    Retries on 429, 502, 503, 504 status codes with exponential backoff
    before giving up.

    Args:
        client: httpx AsyncClient instance
        endpoint: WCS endpoint URL
        params: Request parameters (dict or list of tuples)
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
                    f"WCS request returned retryable status {resp.status_code} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )

                # Don't wait after the last attempt
                if attempt < max_retries - 1:
                    wait_time = WCS_RETRY_WAIT_S * (2 ** attempt)  # Exponential backoff
                    logger.info(f"Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue

            # Non-retryable status code - raise immediately
            resp.raise_for_status()

        except httpx.HTTPStatusError as e:
            last_exception = e
            # If it's a retryable status code and we have retries left, continue
            if e.response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries - 1:
                wait_time = WCS_RETRY_WAIT_S * (2 ** attempt)
                logger.warning(
                    f"WCS request failed with status {e.response.status_code}, "
                    f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)
                continue
            # Otherwise, raise the exception
            raise
        except Exception as e:
            last_exception = e
            logger.error(f"WCS request failed with exception: {e}")
            raise

    # If we get here, all retries were exhausted
    if last_exception:
        raise last_exception

    # This shouldn't happen, but just in case
    raise httpx.HTTPStatusError(
        f"WCS request failed after {max_retries} retries",
        request=resp.request,
        response=resp,
    )


# ---------------------------------------------------------------------------
# WCS fetch functions (version-specific)
# ---------------------------------------------------------------------------

def _parse_wcs_xml_error(xml_bytes: bytes) -> str:
    """
    Parse WCS XML exception/error response to extract error message.

    WCS services return errors in XML format (ServiceExceptionReport or ExceptionReport).
    This function attempts to extract the human-readable error message.
    """
    try:
        import xml.etree.ElementTree as ET
        xml_str = xml_bytes.decode('utf-8', errors='replace')
        root = ET.fromstring(xml_str)

        # Try common WCS error message paths
        # WCS 1.0.0 uses ServiceExceptionReport/ServiceException
        # WCS 1.1.1+ uses ExceptionReport/Exception/ExceptionText
        error_texts = []

        # Look for any text content in exception elements
        for tag in ['ServiceException', 'ExceptionText', 'Exception']:
            for elem in root.iter():
                if tag in elem.tag:
                    if elem.text and elem.text.strip():
                        error_texts.append(elem.text.strip())

        if error_texts:
            return " | ".join(error_texts)

        # Fallback: return first 500 chars of XML
        return xml_str[:500]

    except Exception as e:
        # If XML parsing fails, return the first part of the raw response
        try:
            return xml_bytes.decode('utf-8', errors='replace')[:500]
        except:
            return f"Unable to parse error response (parsing error: {e})"


async def fetch_elevation_wcs_1_0(
    endpoint: str,
    coverage_id: str,
    bbox: tuple[float, float, float, float],
    crs: str,
    width: int,
    height: int,
    auth_params: dict | None = None,
    format: str = "image/tiff",
) -> bytes:
    """Fetch elevation via WCS 1.0.0 (e.g. Norway Kartverket, Denmark Dataforsyningen)."""
    params = {
        "SERVICE": "WCS",
        "VERSION": "1.0.0",
        "REQUEST": "GetCoverage",
        "COVERAGE": coverage_id,
        "CRS": crs,
        "BBOX": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": format,
    }
    if auth_params:
        params.update(auth_params)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await _wcs_request_with_retry(client, endpoint, params)

        # Check if the response is valid TIFF data
        content = resp.content
        content_type = resp.headers.get("content-type", "").lower()

        # Check for error responses (XML, plain text, HTML, etc.)
        if content.startswith(b"<?xml") or content.startswith(b"<"):
            error_msg = _parse_wcs_xml_error(content)
            logger.error(f"WCS 1.0.0 returned XML error: {error_msg}")
            raise ValueError(f"WCS service returned an error: {error_msg}")

        # Check for plain text errors
        if "text/plain" in content_type or content_type.startswith("text/"):
            error_text = content.decode('utf-8', errors='replace')[:500]
            logger.error(f"WCS 1.0.0 returned text error: {error_text}")
            raise ValueError(f"WCS service returned an error: {error_text}")

        # Validate TIFF magic bytes
        if len(content) < 4 or content[:4] not in (b"II*\x00", b"MM\x00*"):
            logger.error(f"WCS 1.0.0 returned invalid TIFF data. First 100 bytes: {content[:100]}")
            logger.error(f"Content-Type: {content_type}")
            raise ValueError(f"WCS service did not return valid TIFF data (Content-Type: {content_type})")

        return content


async def fetch_elevation_wcs_1_1(
    endpoint: str,
    coverage_id: str,
    bbox: tuple[float, float, float, float],
    crs: str,
    width: int,
    height: int,
    auth_params: dict | None = None,
) -> bytes:
    """Fetch elevation via WCS 1.1.1 (e.g. Denmark Dataforsyningen)."""
    params = {
        "service": "WCS",
        "version": "1.1.1",
        "request": "GetCoverage",
        "COVERAGE": coverage_id,
        "FORMAT": "GTiff",
        "CRS": crs,
        "RESPONSE_CRS": crs,
        "BBOX": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
    }
    if auth_params:
        params.update(auth_params)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await _wcs_request_with_retry(client, endpoint, params)

        # Check if the response is XML (error response)
        content = resp.content
        if content.startswith(b"<?xml") or content.startswith(b"<"):
            error_msg = _parse_wcs_xml_error(content)
            logger.error(f"WCS 1.1.1 returned XML error: {error_msg}")
            raise ValueError(f"WCS service returned an error: {error_msg}")

        # WCS 1.1.1 may return multipart MIME; extract TIFF if needed
        content_type = resp.headers.get("content-type", "")
        return _extract_tiff_from_wcs_response(content, content_type)


async def fetch_elevation_wcs_2_0(
    endpoint: str,
    coverage_id: str,
    bbox: tuple[float, float, float, float],
    crs: str,
    axis_labels: tuple[str, str] = ("E", "N"),
    width: int | None = None,
    height: int | None = None,
    auth_params: dict | None = None,
    supports_scalesize: bool = True,
) -> bytes:
    """Fetch elevation via WCS 2.0.1 (e.g. Finland NLS, Estonia Maa-amet)."""
    # Build base parameters
    params = [
        ("SERVICE", "WCS"),
        ("VERSION", "2.0.1"),
        ("REQUEST", "GetCoverage"),
        ("CoverageID", coverage_id),
        ("FORMAT", "image/tiff"),
    ]

    # Add authentication parameters if provided
    if auth_params:
        for key, value in auth_params.items():
            params.append((key, value))

    # Add SUBSET parameters (multiple parameters with same name)
    # WCS 2.0.1 SUBSET format: SUBSET=axisLabel(low,high)
    params.append(("SUBSET", f"{axis_labels[0]}({bbox[0]},{bbox[2]})"))
    params.append(("SUBSET", f"{axis_labels[1]}({bbox[1]},{bbox[3]})"))

    # Add SCALESIZE to limit output dimensions and prevent "Raster size out of range" errors
    # WCS 2.0.1 SCALESIZE format: SCALESIZE=axis1(size1),axis2(size2)
    # Note: SCALESIZE is optional in WCS 2.0.1 and not supported by all servers
    if supports_scalesize and width is not None and height is not None:
        params.append(("SCALESIZE", f"{axis_labels[0]}({width}),{axis_labels[1]}({height})"))

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Use params list to properly handle multiple SUBSET parameters
        resp = await _wcs_request_with_retry(client, endpoint, params)
        logger.debug(f"WCS 2.0.1 request URL: {_sanitize_url(str(resp.request.url))}")

        # Check if the response is XML (error response)
        content = resp.content
        if content.startswith(b"<?xml") or content.startswith(b"<"):
            error_msg = _parse_wcs_xml_error(content)
            logger.error(f"WCS 2.0.1 returned XML error: {error_msg}")
            raise ValueError(f"WCS service returned an error: {error_msg}")

        return content


# ---------------------------------------------------------------------------
# WCS 2.0.1 chunked fetch (for APIs with area limits)
# ---------------------------------------------------------------------------

def _compute_chunks_1d(low: float, high: float, max_span: float) -> list[tuple[float, float]]:
    """Split a 1-D range [low, high] into chunks no wider than *max_span*."""
    span = high - low
    n = math.ceil(span / max_span)
    step = span / n
    chunks = []
    for i in range(n):
        c_low = low + i * step
        c_high = low + (i + 1) * step
        # Clamp the last chunk to the exact boundary to avoid float drift
        if i == n - 1:
            c_high = high
        chunks.append((c_low, c_high))
    return chunks


async def fetch_elevation_wcs_2_0_chunked(
    endpoint: str,
    coverage_id: str,
    bbox: tuple[float, float, float, float],
    crs: str,
    axis_labels: tuple[str, str] = ("E", "N"),
    width: int | None = None,
    height: int | None = None,
    auth_params: dict | None = None,
    supports_scalesize: bool = True,
    max_area_m: int = 10000,
    resolution_m: float = 2.0,
    max_request_px: int = 5000,
) -> bytes:
    """
    Fetch elevation via WCS 2.0.1 with automatic area chunking.

    When the requested bounding box exceeds *max_area_m* on either axis the
    area is split into a grid of tiles, each tile is fetched independently,
    and the results are stitched into a single GeoTIFF in memory.

    Args:
        endpoint:         WCS endpoint URL
        coverage_id:      WCS CoverageID
        bbox:             (min_x, min_y, max_x, max_y) in the native CRS
        crs:              Native CRS string (e.g. EPSG:3067)
        axis_labels:      Axis label pair for SUBSET parameters
        width:            Desired total raster width (pixels)
        height:           Desired total raster height (pixels)
        auth_params:      Authentication query parameters
        supports_scalesize: Whether the server supports SCALESIZE
        max_area_m:       Maximum metres per SUBSET axis before chunking
        resolution_m:     Native pixel resolution (for computing per-chunk px)
        max_request_px:   Maximum pixel dimension per single request

    Returns:
        Merged GeoTIFF bytes
    """
    x_min, y_min, x_max, y_max = bbox
    x_span = x_max - x_min
    y_span = y_max - y_min

    # Do we actually need chunking?
    if x_span <= max_area_m and y_span <= max_area_m:
        # Small enough for a single request
        return await fetch_elevation_wcs_2_0(
            endpoint, coverage_id, bbox, crs,
            axis_labels=axis_labels, width=width, height=height,
            auth_params=auth_params, supports_scalesize=supports_scalesize,
        )

    # Compute chunk grid
    x_chunks = _compute_chunks_1d(x_min, x_max, max_area_m)
    y_chunks = _compute_chunks_1d(y_min, y_max, max_area_m)
    n_tiles = len(x_chunks) * len(y_chunks)
    logger.info(
        f"Area {x_span:.0f}×{y_span:.0f} m exceeds {max_area_m} m limit — "
        f"splitting into {len(x_chunks)}×{len(y_chunks)} = {n_tiles} tile(s)"
    )

    # Fetch each tile ---------------------------------------------------------
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.merge import merge as rasterio_merge

    tile_datasets = []
    tile_counter = 0
    try:
        for yi, (cy_lo, cy_hi) in enumerate(y_chunks):
            for xi, (cx_lo, cx_hi) in enumerate(x_chunks):
                # Throttle: small delay between chunk requests to avoid
                # overwhelming country elevation APIs.
                if tile_counter > 0:
                    await asyncio.sleep(0.3)
                tile_counter += 1

                chunk_bbox = (cx_lo, cy_lo, cx_hi, cy_hi)

                # Per-chunk pixel dimensions (proportional, clamped to server max)
                chunk_w = min(max_request_px, max(64, int((cx_hi - cx_lo) / resolution_m)))
                chunk_h = min(max_request_px, max(64, int((cy_hi - cy_lo) / resolution_m)))

                logger.info(
                    f"  Fetching tile [{yi},{xi}] "
                    f"E({cx_lo:.0f},{cx_hi:.0f}) N({cy_lo:.0f},{cy_hi:.0f}) "
                    f"→ {chunk_w}×{chunk_h} px"
                )

                tiff_bytes = await fetch_elevation_wcs_2_0(
                    endpoint, coverage_id, chunk_bbox, crs,
                    axis_labels=axis_labels,
                    width=chunk_w, height=chunk_h,
                    auth_params=auth_params,
                    supports_scalesize=supports_scalesize,
                )

                # Open as an in-memory rasterio dataset for merging
                memfile = rasterio.MemoryFile(tiff_bytes)
                ds = memfile.open()
                tile_datasets.append((memfile, ds))

        # Merge all tiles into a single raster --------------------------------
        datasets = [ds for _, ds in tile_datasets]
        merged_array, merged_transform = rasterio_merge(datasets)

        # If the caller requested a specific output size, resample
        out_h, out_w = merged_array.shape[1], merged_array.shape[2]
        if width and height and (out_w != width or out_h != height):
            from rasterio.enums import Resampling
            # Compute resampled transform
            target_transform = from_bounds(
                x_min, y_min, x_max, y_max, width, height
            )
            resampled = np.empty((merged_array.shape[0], height, width), dtype=merged_array.dtype)
            rasterio.warp.reproject(
                merged_array, resampled,
                src_transform=merged_transform,
                src_crs=crs,
                dst_transform=target_transform,
                dst_crs=crs,
                resampling=Resampling.bilinear,
                num_threads=os.cpu_count() or 2,
            )
            merged_array = resampled
            merged_transform = target_transform
            out_h, out_w = height, width

        # Write merged GeoTIFF to bytes
        profile = datasets[0].profile.copy()
        profile.update(
            width=out_w,
            height=out_h,
            transform=merged_transform,
            count=merged_array.shape[0],
        )
        output = io.BytesIO()
        with rasterio.open(output, "w", **profile) as dst:
            dst.write(merged_array)
        merged_bytes = output.getvalue()

        logger.info(
            f"Merged {n_tiles} tile(s) into {out_w}×{out_h} px GeoTIFF "
            f"({len(merged_bytes)} bytes)"
        )
        return merged_bytes

    finally:
        # Clean up all in-memory datasets
        for memfile, ds in tile_datasets:
            try:
                ds.close()
                memfile.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# OpenTopography global fallback
# ---------------------------------------------------------------------------

async def fetch_elevation_opentopography(
    bbox: dict,
    dem_type: str = "COP30",
    api_key: str | None = None,
) -> Optional[bytes]:
    """
    Fetch elevation data from OpenTopography Global DEM API.

    Supports: COP30 (Copernicus 30 m), SRTMGL1 (SRTM 30 m), AW3D30 (ALOS 30 m).

    Args:
        bbox: Dict with west, south, east, north in EPSG:4326
        dem_type: DEM dataset identifier
        api_key: OpenTopography API key

    Returns:
        GeoTIFF bytes or None on failure
    """
    if api_key is None:
        api_key = OPENTOPOGRAPHY_API_KEY

    if not api_key:
        logger.error(
            "OpenTopography API key not configured. "
            "Register for free at https://portal.opentopography.org/ "
            "and set OPENTOPOGRAPHY_API_KEY in your .env file"
        )
        return None

    params = {
        "demtype": dem_type,
        "south": bbox["south"],
        "north": bbox["north"],
        "west": bbox["west"],
        "east": bbox["east"],
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }

    logger.info(f"Fetching {dem_type} from OpenTopography: {bbox}")

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await _wcs_request_with_retry(client, OPENTOPOGRAPHY_ENDPOINT, params)
            content_type = resp.headers.get("content-type", "")
            if "tiff" in content_type or "octet-stream" in content_type or len(resp.content) > 1000:
                logger.info(f"Received {len(resp.content)} bytes of elevation data")
                return resp.content
            else:
                logger.error(f"OpenTopography returned non-TIFF response: {resp.text[:500]}")
                return None
        except Exception as e:
            logger.error(f"OpenTopography request failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_tiff_from_wcs_response(data: bytes, content_type: str) -> bytes:
    """
    Extract GeoTIFF from WCS multipart response if needed.

    TIFF files start with magic bytes:
    - Little-endian: b'II*\x00' (0x49 0x49 0x2A 0x00)
    - Big-endian: b'MM\x00*' (0x4D 0x4D 0x00 0x2A)
    """
    # Check if data already starts with valid TIFF magic bytes
    if len(data) >= 4:
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            logger.debug("Data already contains valid TIFF magic bytes")
            return data

    # Try to extract from multipart MIME response
    if "multipart" in content_type.lower():
        logger.debug(f"Parsing multipart response, content-type: {content_type}")
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part.split("=", 1)[1].strip('"').strip("'")
                break

        if boundary:
            logger.debug(f"Found boundary: {boundary}")
            # Split by boundary marker
            parts = data.split(f"--{boundary}".encode())
            logger.debug(f"Split into {len(parts)} parts")

            for i, part in enumerate(parts):
                # Look for TIFF data
                # Check for content-type header indicating TIFF
                if b"image/tiff" in part.lower() or b"application/tiff" in part.lower():
                    logger.debug(f"Found TIFF content-type in part {i}")
                    # Find the end of headers (double newline)
                    header_end = part.find(b"\r\n\r\n")
                    if header_end > 0:
                        tiff_data = part[header_end + 4:]
                        if len(tiff_data) >= 4 and tiff_data[:4] in (b"II*\x00", b"MM\x00*"):
                            logger.info(f"Successfully extracted TIFF data from multipart ({len(tiff_data)} bytes)")
                            return tiff_data
                    header_end = part.find(b"\n\n")
                    if header_end > 0:
                        tiff_data = part[header_end + 2:]
                        if len(tiff_data) >= 4 and tiff_data[:4] in (b"II*\x00", b"MM\x00*"):
                            logger.info(f"Successfully extracted TIFF data from multipart ({len(tiff_data)} bytes)")
                            return tiff_data

                # Also check if this part contains TIFF magic bytes directly
                tiff_le_pos = part.find(b"II*\x00")
                tiff_be_pos = part.find(b"MM\x00*")
                if tiff_le_pos >= 0:
                    logger.info(f"Found little-endian TIFF magic bytes at position {tiff_le_pos} in part {i}")
                    return part[tiff_le_pos:]
                if tiff_be_pos >= 0:
                    logger.info(f"Found big-endian TIFF magic bytes at position {tiff_be_pos} in part {i}")
                    return part[tiff_be_pos:]
        else:
            logger.warning("Multipart content-type but no boundary found")

    # If we get here, we couldn't extract TIFF data properly
    # Log the first 100 bytes to help debug
    logger.warning(f"Could not extract valid TIFF data. First 100 bytes: {data[:100]}")
    logger.warning(f"Content-Type: {content_type}")

    return data


def transform_bbox_to_country_crs(
    bbox_wgs84: tuple[float, float, float, float],
    target_crs: str,
) -> tuple[float, float, float, float]:
    """Transform a WGS84 bbox (west, south, east, north) to a target CRS."""
    return transform_bbox_to_crs(bbox_wgs84, target_crs)


# ---------------------------------------------------------------------------
# Country-specific elevation dispatcher
# ---------------------------------------------------------------------------

async def fetch_elevation_for_country(
    country_code: str,
    bbox_wgs84: tuple[float, float, float, float],
    target_width: int,
    target_height: int,
) -> Optional[bytes]:
    """
    Fetch elevation GeoTIFF for a given country and bounding box.

    Dispatches to the correct WCS version based on the country's config.

    Args:
        country_code: ISO 3166-1 alpha-2 code
        bbox_wgs84: (west, south, east, north) in WGS84
        target_width: Desired raster width in pixels
        target_height: Desired raster height in pixels

    Returns:
        GeoTIFF bytes, or None if unavailable.
    """
    config = ELEVATION_CONFIGS.get(country_code)
    if config is None:
        logger.warning(f"No elevation config for country {country_code}")
        return None

    native_crs = config.native_crs
    bbox_native = transform_bbox_to_country_crs(bbox_wgs84, native_crs)

    # Build auth params
    auth_params: dict = {}
    if config.auth_type == "token":
        token = os.environ.get(config.auth_env_var, "")
        if token:
            auth_params["token"] = token
        else:
            logger.warning(f"No token configured for {config.name} ({config.auth_env_var})")
            return None
    elif config.auth_type == "api_key":
        api_key = os.environ.get(config.auth_env_var, "")
        if api_key:
            auth_params["api-key"] = api_key
        else:
            logger.warning(f"No API key configured for {config.name} ({config.auth_env_var})")
            return None
    elif config.auth_type == "basic":
        import base64
        username = os.environ.get(config.auth_env_var, "")
        password_env = config.extra_params.get("password_env_var", "")
        password = os.environ.get(password_env, "") if password_env else ""
        if not username or not password:
            logger.warning(
                f"No credentials configured for {config.name} "
                f"({config.auth_env_var} / {password_env})"
            )
            return None
        # Basic auth is passed as an HTTP header, not query params.
        # For STAC APIs, the auth is handled by the STAC fetcher directly.
        # For WCS APIs (if any used basic auth), we'd need to pass headers.
        credentials = f"{username}:{password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        auth_params["_basic_auth_header"] = f"Basic {encoded}"

    try:
        # STAC-based APIs (e.g. Lantmäteriet STAC Höjd)
        if config.api_type == "stac":
            from services.lantmateriet.stac_elevation import fetch_stac_elevation

            logger.info(f"Fetching STAC elevation for {config.name}")
            return await fetch_stac_elevation(
                bbox_native,
                crs=native_crs,
                target_width=target_width,
                target_height=target_height,
            )

        # WCS-based APIs
        if config.version == "1.0.0":
            return await fetch_elevation_wcs_1_0(
                config.endpoint, config.coverage_id,
                bbox_native, native_crs,
                target_width, target_height,
                auth_params or None,
                format=config.format,
            )
        elif config.version == "1.1.1":
            return await fetch_elevation_wcs_1_1(
                config.endpoint, config.coverage_id,
                bbox_native, native_crs,
                target_width, target_height,
                auth_params or None,
            )
        elif config.version == "2.0.1":
            # Default axis labels for WCS 2.0.1
            axis_labels = ("E", "N")
            # Projected coordinate systems use X,Y axis labels
            # Estonia uses X,Y for EPSG:3301
            # Poland uses X,Y for EPSG:2180
            if country_code in ("EE", "PL"):
                axis_labels = ("X", "Y")

            # Use chunked fetch if the API has an area limit and the
            # request exceeds it on either axis
            if config.max_area_m > 0:
                x_span = bbox_native[2] - bbox_native[0]
                y_span = bbox_native[3] - bbox_native[1]
                if x_span > config.max_area_m or y_span > config.max_area_m:
                    logger.info(
                        f"{config.name} area {x_span:.0f}×{y_span:.0f} m "
                        f"exceeds limit {config.max_area_m} m — using chunked fetch"
                    )
                    return await fetch_elevation_wcs_2_0_chunked(
                        config.endpoint, config.coverage_id,
                        bbox_native, native_crs,
                        axis_labels=axis_labels,
                        width=target_width,
                        height=target_height,
                        auth_params=auth_params or None,
                        supports_scalesize=config.supports_scalesize,
                        max_area_m=config.max_area_m,
                        resolution_m=config.resolution_m,
                        max_request_px=config.max_request_size,
                    )

            return await fetch_elevation_wcs_2_0(
                config.endpoint, config.coverage_id,
                bbox_native, native_crs,
                axis_labels=axis_labels,
                width=target_width,
                height=target_height,
                auth_params=auth_params or None,
                supports_scalesize=config.supports_scalesize,
            )
        else:
            logger.warning(f"Unsupported WCS version {config.version} for {config.name}")
            return None

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching elevation from {config.name}: {e.response.status_code}")
        logger.error(f"Request URL: {_sanitize_url(str(e.request.url))}")
        if e.response.text:
            logger.error(f"Response body: {e.response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Error fetching elevation from {config.name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main elevation entry point (tries country WCS then OpenTopography)
# ---------------------------------------------------------------------------

async def fetch_elevation(
    bbox: dict,
    country_code: str,
    job = None,
    target_resolution_m: float = 30,
) -> dict:
    """
    Main entry point: fetch elevation data using the best available source.

    Tries country-specific high-resolution source first, then falls back
    to Copernicus DEM 30 m via OpenTopography.

    Args:
        bbox: Dict with west, south, east, north in EPSG:4326
        country_code: ISO 2-letter country code
        job: Optional MapGenerationJob for logging
        target_resolution_m: Desired resolution (informational)

    Returns:
        Dict with data (GeoTIFF bytes), source, resolution_m, crs.
    """
    result: dict = {
        "data": None,
        "source": None,
        "resolution_m": None,
        "crs": None,
    }

    # Convert bbox dict to tuple for country-specific fetcher
    bbox_tuple = (bbox["west"], bbox["south"], bbox["east"], bbox["north"])

    # Estimate pixel dimensions from bbox and resolution
    width_m, height_m = estimate_bbox_dimensions_m(bbox)

    config = ELEVATION_CONFIGS.get(country_code)
    if config:
        native_res = config.resolution_m
        max_size = config.max_request_size
        target_w = min(max_size, max(64, int(width_m / native_res)))
        target_h = min(max_size, max(64, int(height_m / native_res)))

        logger.info(f"Attempting {config.name} ({native_res} m)...")
        if job:
            # Inform the user if chunking will be needed
            if config.max_area_m > 0 and (width_m > config.max_area_m or height_m > config.max_area_m):
                n_x = math.ceil(width_m / config.max_area_m)
                n_y = math.ceil(height_m / config.max_area_m)
                job.add_log(
                    f"Attempting {config.name} ({native_res}m) — area {width_m:.0f}×{height_m:.0f}m "
                    f"exceeds {config.max_area_m}m limit, will fetch in {n_x}×{n_y} tiles..."
                )
            else:
                job.add_log(f"Attempting to fetch elevation data from {config.name} ({native_res}m resolution)...")
            job.progress = 12
        data = await fetch_elevation_for_country(country_code, bbox_tuple, target_w, target_h)
        if data:
            result["data"] = data
            result["source"] = f"{config.name} ({native_res} m)"
            result["resolution_m"] = native_res
            result["crs"] = config.native_crs
            if job:
                job.add_log(f"Successfully fetched elevation data from {config.name} ({len(data)} bytes)", "success")
                job.progress = 23
            return result
        else:
            if job:
                job.add_log(f"Failed to fetch from {config.name}, will try fallback source...", "warning")

    # Fallback: OpenTopography Copernicus DEM 30 m
    logger.info("Using OpenTopography Copernicus DEM 30 m fallback...")
    if job:
        job.add_log("Using OpenTopography Copernicus DEM 30m as fallback...")
        job.progress = 15
    data = await fetch_elevation_opentopography(bbox, "COP30")
    if data:
        result["data"] = data
        result["source"] = "Copernicus DEM GLO-30 (OpenTopography)"
        result["resolution_m"] = 30
        result["crs"] = "EPSG:4326"
        if job:
            job.add_log(f"Successfully fetched elevation data from OpenTopography ({len(data)} bytes)", "success")
            job.progress = 23
        return result

    # SRTM fallback (only works below 60 deg N)
    if bbox["north"] < 60:
        logger.info("Trying SRTM 30 m as last resort...")
        data = await fetch_elevation_opentopography(bbox, "SRTMGL1")
        if data:
            result["data"] = data
            result["source"] = "SRTM GL1 30 m (OpenTopography)"
            result["resolution_m"] = 30
            result["crs"] = "EPSG:4326"
            return result

    # ALOS fallback
    logger.info("Trying ALOS World 3D 30 m...")
    data = await fetch_elevation_opentopography(bbox, "AW3D30")
    if data:
        result["data"] = data
        result["source"] = "ALOS World 3D 30 m (OpenTopography)"
        result["resolution_m"] = 30
        result["crs"] = "EPSG:4326"
        return result

    logger.error("All elevation sources failed!")
    return result


