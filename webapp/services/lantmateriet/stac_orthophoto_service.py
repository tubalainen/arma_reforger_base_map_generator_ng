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
) -> tuple[Optional[np.ndarray], object]:
    """
    Open COG tiles via VSICURL and merge the RGB bands over the target bbox.

    Uses rasterio.merge() with a bounds + resolution constraint so that GDAL
    issues HTTP range requests only for the pixels and overview level that
    correspond to our target output size — typically a few MB rather than the
    full 460 MB tile.

    Args:
        vsicurl_hrefs: List of '/vsicurl/https://dl1.lantmateriet.se/...' paths.
        epsg3006_bounds: (x_min, y_min, x_max, y_max) in EPSG:3006 (metres).
        target_width: Output pixel width.
        target_height: Output pixel height.

    Returns:
        (merged_rgb_array, affine_transform) where the array has shape
        (3, H, W) in uint8. Returns (None, None) on failure.
    """
    import rasterio
    from rasterio.env import Env
    from rasterio.enums import Resampling
    from rasterio.merge import merge as rasterio_merge

    x_min, y_min, x_max, y_max = epsg3006_bounds
    # Target resolution in metres/pixel — rasterio.merge selects the closest
    # COG overview level automatically
    res_x = (x_max - x_min) / target_width
    res_y = (y_max - y_min) / target_height

    datasets = []
    with Env(**_gdal_vsicurl_env()):
        # Open all COG files (only the header is fetched at this point)
        for href in vsicurl_hrefs:
            try:
                ds = rasterio.open(href)
                datasets.append(ds)
            except Exception as exc:
                logger.warning(f"Could not open COG {href}: {exc}")

        if not datasets:
            return None, None

        try:
            # merge() with bounds + res triggers windowed COG reads:
            # only pixels within epsg3006_bounds at ~res_x m/px are fetched.
            # indexes=[1, 2, 3] selects the RGB bands (band 4 is NIR — skip it).
            merged, transform = rasterio_merge(
                datasets,
                bounds=(x_min, y_min, x_max, y_max),
                res=(res_x, res_y),
                resampling=Resampling.bilinear,
                indexes=[1, 2, 3],
                nodata=0,
            )
            # merged shape: (3, H, W) — may differ slightly from target due to
            # COG overview snapping; caller resamples to exact size
        except Exception as exc:
            logger.error(f"rasterio.merge failed for STAC Bild COGs: {exc}")
            return None, None
        finally:
            for ds in datasets:
                try:
                    ds.close()
                except Exception:
                    pass

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
    # 3. Convert WGS84 bbox to EPSG:3006 for the COG bounds query
    # ------------------------------------------------------------------ #
    try:
        from pyproj import Transformer

        to_3006 = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True)
        x_min, y_min = to_3006.transform(w, s)
        x_max, y_max = to_3006.transform(e, n)
    except Exception as exc:
        logger.error(f"STAC Bild: EPSG:4326 → EPSG:3006 transform failed: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # 4. COG windowed merge (synchronous; rasterio issues HTTP range reqs)
    # ------------------------------------------------------------------ #
    merged_rgb, src_transform = _cog_merge_rgb(
        vsicurl_hrefs,
        epsg3006_bounds=(x_min, y_min, x_max, y_max),
        target_width=width,
        target_height=height,
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
        from rasterio.crs import CRS
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds
        from rasterio.warp import reproject as warp_reproject

        src_crs = CRS.from_epsg(3006)
        dst_crs = CRS.from_epsg(4326)
        dst_transform = from_bounds(w, s, e, n, width, height)
        dst_array = np.zeros((3, height, width), dtype=np.uint8)

        for band in range(3):
            warp_reproject(
                source=merged_rgb[band],
                destination=dst_array[band],
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )

    except Exception as exc:
        logger.error(f"STAC Bild: EPSG:3006 → WGS84 warp failed: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # 6. Encode as PNG and return
    # ------------------------------------------------------------------ #
    try:
        from PIL import Image

        img = Image.fromarray(dst_array.transpose(1, 2, 0))  # (H, W, 3)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        logger.info(
            f"STAC Bild: orthophoto ready — {width}×{height} px, "
            f"{len(png_bytes) / 1024:.0f} KB, imagery year: {newest_year}"
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
