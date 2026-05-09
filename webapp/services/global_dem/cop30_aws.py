"""
Copernicus DEM GLO-30 (COP30) direct fetch from AWS Open Data.

Worldwide 30 m elevation, no API key required. Identical pixel data to what
OpenTopography serves for `demtype=COP30`, but accessed directly from the
AWS Open Data bucket (`copernicus-dem-30m`) so there's no rate limit and no
`OPENTOPOGRAPHY_API_KEY` dependency.

Tiles are 1°×1° COGs anchored at their SW corner. URL pattern:

    https://copernicus-dem-30m.s3.amazonaws.com/
        Copernicus_DSM_COG_10_<lat>_00_<lon>_00_DEM/
        Copernicus_DSM_COG_10_<lat>_00_<lon>_00_DEM.tif

Where <lat> is `N00`–`N89` / `S01`–`S90` (zero-padded 2 digits) and
<lon> is `E000`–`E179` / `W001`–`W180` (zero-padded 3 digits).

Tiles over open ocean (no land in the cell) simply don't exist — a 404 is
expected and treated as "no data here" rather than an error.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

COP30_AWS_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
TILE_REQUEST_TIMEOUT = 60.0
MAX_CONCURRENT_DOWNLOADS = 8


def _tile_id(lat_int: int, lon_int: int) -> str:
    """Return the COP30 tile basename for the 1°×1° cell anchored at (lat_int, lon_int)."""
    lat_str = f"N{lat_int:02d}" if lat_int >= 0 else f"S{abs(lat_int):02d}"
    lon_str = f"E{lon_int:03d}" if lon_int >= 0 else f"W{abs(lon_int):03d}"
    return f"Copernicus_DSM_COG_10_{lat_str}_00_{lon_str}_00_DEM"


def _tile_url(lat_int: int, lon_int: int) -> str:
    tid = _tile_id(lat_int, lon_int)
    return f"{COP30_AWS_BASE}/{tid}/{tid}.tif"


def _tiles_intersecting_bbox(bbox: dict) -> list[tuple[int, int]]:
    """
    Return list of (lat_int, lon_int) tile origins that intersect the bbox.

    Each tile covers [lat, lat+1) × [lon, lon+1) so we floor the south/west
    edges and ceil the north/east edges.
    """
    lat_min = math.floor(bbox["south"])
    lat_max = math.ceil(bbox["north"])
    lon_min = math.floor(bbox["west"])
    lon_max = math.ceil(bbox["east"])

    # If the bbox edge sits exactly on a tile boundary, ceil() would add an
    # extra empty row/column — clamp it back.
    if lat_max == lat_min:
        lat_max += 1
    if lon_max == lon_min:
        lon_max += 1

    return [(lat, lon) for lat in range(lat_min, lat_max) for lon in range(lon_min, lon_max)]


async def _fetch_tile(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    lat_int: int,
    lon_int: int,
) -> Optional[bytes]:
    url = _tile_url(lat_int, lon_int)
    async with semaphore:
        try:
            resp = await client.get(url, timeout=TILE_REQUEST_TIMEOUT)
        except httpx.HTTPError as e:
            logger.warning(f"COP30 tile request failed ({lat_int},{lon_int}): {e}")
            return None

    if resp.status_code == 404:
        logger.debug(
            f"COP30 tile not present at {lat_int},{lon_int} "
            f"(open ocean cells have no tile)"
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            f"COP30 tile {lat_int},{lon_int} returned HTTP {resp.status_code}"
        )
        return None

    content = resp.content
    if len(content) < 4 or content[:4] not in (b"II*\x00", b"MM\x00*"):
        logger.warning(
            f"COP30 tile {lat_int},{lon_int} did not return valid TIFF "
            f"(first 8 bytes: {content[:8]!r})"
        )
        return None

    return content


async def fetch_elevation_cop30_aws(
    bbox: dict,
    job=None,
) -> Optional[bytes]:
    """
    Fetch COP30 elevation directly from AWS Open Data.

    Args:
        bbox: dict with west, south, east, north in EPSG:4326
        job:  optional MapGenerationJob for progress logging

    Returns:
        Merged GeoTIFF bytes cropped to the requested bbox, or None on failure
        (e.g. no land cells in the bbox, or all tile requests failed).
    """
    tiles = _tiles_intersecting_bbox(bbox)
    if not tiles:
        logger.error(f"COP30: no tiles intersect bbox {bbox}")
        return None

    n_tiles = len(tiles)
    logger.info(f"COP30 AWS: fetching {n_tiles} tile(s) for bbox {bbox}")
    if job:
        job.add_log(
            f"Downloading {n_tiles} Copernicus DEM tile(s) from AWS Open Data..."
        )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tile_bytes = await asyncio.gather(
            *[_fetch_tile(client, semaphore, lat, lon) for lat, lon in tiles]
        )

    valid = [(t, b) for t, b in zip(tiles, tile_bytes) if b is not None]
    if not valid:
        logger.error(
            f"COP30 AWS: all {n_tiles} tile fetches failed (or area is "
            f"entirely open ocean with no tiles)"
        )
        return None

    if len(valid) < n_tiles:
        logger.info(
            f"COP30 AWS: {len(valid)}/{n_tiles} tile(s) returned data — "
            f"the rest are likely ocean cells without tiles"
        )

    return _merge_and_crop(valid, bbox)


def _merge_and_crop(valid_tiles: list[tuple[tuple[int, int], bytes]], bbox: dict) -> Optional[bytes]:
    """Merge tile bytes via rasterio and crop to bbox; return GeoTIFF bytes."""
    import rasterio
    from rasterio.merge import merge as rasterio_merge

    memfiles: list = []
    datasets: list = []
    try:
        for _, tiff_bytes in valid_tiles:
            mf = rasterio.MemoryFile(tiff_bytes)
            ds = mf.open()
            memfiles.append(mf)
            datasets.append(ds)

        merged_arr, merged_transform = rasterio_merge(
            datasets,
            bounds=(bbox["west"], bbox["south"], bbox["east"], bbox["north"]),
        )

        profile = datasets[0].profile.copy()
        profile.update(
            driver="GTiff",
            width=merged_arr.shape[2],
            height=merged_arr.shape[1],
            transform=merged_transform,
            count=merged_arr.shape[0],
        )

        out = io.BytesIO()
        with rasterio.open(out, "w", **profile) as dst:
            dst.write(merged_arr)
        result = out.getvalue()

        logger.info(
            f"COP30 AWS: merged {len(valid_tiles)} tile(s) into "
            f"{merged_arr.shape[2]}×{merged_arr.shape[1]} px GeoTIFF "
            f"({len(result)} bytes)"
        )
        return result
    except Exception as e:
        logger.error(f"COP30 AWS: failed to merge tiles: {e}")
        return None
    finally:
        for ds in datasets:
            try:
                ds.close()
            except Exception:
                pass
        for mf in memfiles:
            try:
                mf.close()
            except Exception:
                pass
