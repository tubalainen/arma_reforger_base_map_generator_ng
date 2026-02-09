"""
Lantmäteriet STAC Höjd (elevation) service.

Fetches high-resolution elevation data (1 m LiDAR) from the
Lantmäteriet STAC API. Returns GeoTIFF bytes compatible with
the existing elevation_service.py pipeline.

Architecture notes:
- The STAC catalog (api.lantmateriet.se) is OPEN — no auth needed for search.
- The asset downloads (dl1.lantmateriet.se) require HTTP Basic Auth.
- Tiles are 2.5 km × 2.5 km at 1 m resolution (2500×2500 px) in EPSG:5845.
- Collection IDs follow the pattern "mhm-{grid_ref}" (e.g., "mhm-65_5").
- Each item has a "data" asset (COG GeoTIFF), "metadata" (JSON), "thumbnail" (JPEG).

Memory management:
- For large areas (20 km × 20 km), up to ~63 tiles may be needed.
- Each tile is ~10 MB (2500×2500 × float32), so holding all tiles in
  memory simultaneously would require ~1.5 GB+ just for raw data, plus
  the merged mosaic and reproject buffers can push peak usage over 4 GB.
- To avoid OOM kills (exit code 137), tiles are written to temporary
  files on disk and opened as disk-backed rasterio datasets. The merge
  and reproject pipeline then streams from disk rather than holding
  everything in RAM at once.

Workflow:
1. Convert native CRS bbox to WGS84 (STAC spec requires WGS84 bbox)
2. Query the STAC search endpoint for items intersecting the bbox
3. Download COG GeoTIFF assets with Basic Auth (saved to temp files)
4. Merge tiles from disk and crop to requested bbox
5. Optionally resample to requested dimensions
6. Return merged GeoTIFF bytes
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

from config.lantmateriet import LANTMATERIET_CONFIG
from services.lantmateriet.auth import get_basic_auth_header

logger = logging.getLogger(__name__)


def _get_download_headers() -> dict:
    """
    Get headers for downloading assets from dl1.lantmateriet.se.

    The download endpoint requires Basic Auth but expects binary data,
    so we set Accept to wildcard (not application/json).
    """
    headers = {
        "User-Agent": "ArmaReforgerMapGenerator/1.0",
        "Accept": "*/*",
    }
    auth = get_basic_auth_header()
    if auth:
        headers.update(auth)
    return headers


async def fetch_stac_elevation(
    bbox_native: tuple[float, float, float, float],
    crs: str = "EPSG:3006",
    target_width: int | None = None,
    target_height: int | None = None,
) -> Optional[bytes]:
    """
    Fetch elevation data from Lantmäteriet STAC Höjd API.

    Args:
        bbox_native: (min_x, min_y, max_x, max_y) in the native CRS (EPSG:3006)
        crs: Native CRS string (default: EPSG:3006 — SWEREF99 TM)
        target_width: Desired raster width in pixels (or None for native)
        target_height: Desired raster height in pixels (or None for native)

    Returns:
        GeoTIFF bytes covering the bbox, or None on failure.
    """
    if not LANTMATERIET_CONFIG.has_credentials():
        logger.error("Cannot fetch STAC elevation: no authentication credentials")
        return None

    download_headers = _get_download_headers()
    if "Authorization" not in download_headers:
        logger.error("Cannot fetch STAC elevation: auth header generation failed")
        return None

    search_url = f"{LANTMATERIET_CONFIG.stac_hojd_endpoint}search"

    # STAC search: bbox must be WGS84 per STAC specification.
    # Convert from native CRS (EPSG:3006) to EPSG:4326.
    from pyproj import Transformer

    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x_min, y_min, x_max, y_max = bbox_native
    lon_min, lat_min = transformer.transform(x_min, y_min)
    lon_max, lat_max = transformer.transform(x_max, y_max)
    bbox_wgs84 = [lon_min, lat_min, lon_max, lat_max]

    # STAC search — no auth needed for the catalog API
    search_headers = {
        "User-Agent": "ArmaReforgerMapGenerator/1.0",
        "Accept": "application/json",
    }

    query = {
        "bbox": bbox_wgs84,
        "limit": 100,
        # Don't filter by collection — let the API return all matching
        # collections (mhm-*) for our bbox
    }

    # Create a temporary directory for tile files (cleaned up in finally block)
    tmp_dir = tempfile.mkdtemp(prefix="stac_elev_")

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            # 1. Search for STAC items covering the bbox
            logger.info(
                f"STAC Höjd: searching for elevation items "
                f"(bbox WGS84: [{lon_min:.4f}, {lat_min:.4f}, {lon_max:.4f}, {lat_max:.4f}])"
            )
            resp = await client.post(search_url, json=query, headers=search_headers)
            resp.raise_for_status()

            stac_result = resp.json()
            features = stac_result.get("features", [])

            if not features:
                logger.warning("No elevation data found in STAC Höjd search")
                return None

            logger.info(f"STAC Höjd returned {len(features)} item(s)")

            # 2. Download COG assets to temporary files on disk
            #    This avoids holding all tiles in memory simultaneously,
            #    which would cause OOM for large areas (63 tiles × ~10 MB each).
            import rasterio
            import rasterio.warp
            from rasterio.merge import merge as rasterio_merge
            from rasterio.transform import from_bounds

            tile_paths: list[Path] = []
            download_errors = 0

            for tile_idx, feat in enumerate(features):
                assets = feat.get("assets", {})
                data_asset = assets.get("data")
                if data_asset is None:
                    continue

                # Throttle: small delay between tile downloads to be
                # gentle on Lantmäteriet's download server.
                if tile_idx > 0:
                    await asyncio.sleep(0.5)

                asset_url = data_asset["href"]
                asset_size = data_asset.get("file:size", "unknown")
                tile_id = feat.get("id", f"tile_{tile_idx}")
                logger.info(
                    f"Downloading STAC tile: {tile_id} "
                    f"({asset_size} bytes) from {asset_url}"
                )

                # Download with Basic Auth — dl1.lantmateriet.se requires it
                data_resp = await client.get(
                    asset_url,
                    headers=download_headers,
                    follow_redirects=True,
                )

                if data_resp.status_code != 200:
                    if data_resp.status_code == 403:
                        logger.error(
                            f"STAC Höjd tile download returned HTTP 403 Forbidden. "
                            f"Your Lantmäteriet credentials are recognized but your "
                            f"account is NOT authorized for Höjddata downloads. "
                            f"You may need to subscribe to the 'Höjddata' product at "
                            f"https://apimanager.lantmateriet.se/"
                        )
                        # All tiles will fail with the same error — abort early
                        return None
                    elif data_resp.status_code == 401:
                        logger.error(
                            f"STAC Höjd tile download returned HTTP 401 Unauthorized. "
                            f"Check LANTMATERIET_USERNAME and LANTMATERIET_PASSWORD in .env"
                        )
                        return None
                    logger.warning(
                        f"Failed to download tile {tile_id}: "
                        f"HTTP {data_resp.status_code}"
                    )
                    download_errors += 1
                    continue

                # Validate TIFF magic bytes
                content = data_resp.content
                if len(content) < 4 or content[:4] not in (b"II*\x00", b"MM\x00*"):
                    logger.warning(
                        f"STAC asset is not valid TIFF data "
                        f"(first 20 bytes: {content[:20]!r}), skipping"
                    )
                    download_errors += 1
                    continue

                # Write tile to a temporary file on disk instead of
                # keeping it in a rasterio.MemoryFile.
                tile_path = Path(tmp_dir) / f"{tile_id}.tif"
                tile_path.write_bytes(content)
                tile_paths.append(tile_path)

                # Quick validation — open and close immediately
                with rasterio.open(tile_path) as ds:
                    logger.info(
                        f"  Tile OK: {ds.width}x{ds.height} px, "
                        f"CRS={ds.crs}, {len(content)} bytes"
                    )

                # Release the download content from memory
                del content

            if not tile_paths:
                if download_errors > 0:
                    logger.error(
                        f"All {download_errors} tile downloads failed. "
                        f"Check Lantmäteriet credentials (LANTMATERIET_USERNAME/PASSWORD)."
                    )
                else:
                    logger.warning("No usable assets found in STAC results")
                return None

            logger.info(
                f"Downloaded {len(tile_paths)} tiles to disk "
                f"({download_errors} failed)"
            )

            # 3. Merge all tiles from disk-backed datasets
            #    Open datasets from files (disk-backed, not in-memory).
            datasets = [rasterio.open(p) for p in tile_paths]
            try:
                # Detect the source CRS from the first tile
                # (Lantmäteriet tiles use EPSG:5845 = SWEREF 99 TM + RH 2000)
                src_crs = str(datasets[0].crs)
                logger.info(f"Source tile CRS: {src_crs}")

                if len(datasets) == 1:
                    merged_array = datasets[0].read()
                    merged_transform = datasets[0].transform
                    profile = datasets[0].profile.copy()
                else:
                    # rasterio_merge reads from disk-backed datasets,
                    # only the output merged array lives in memory.
                    merged_array, merged_transform = rasterio_merge(datasets)
                    profile = datasets[0].profile.copy()
                    profile.update(
                        width=merged_array.shape[2],
                        height=merged_array.shape[1],
                        transform=merged_transform,
                        count=merged_array.shape[0],
                    )

                logger.info(
                    f"Merged {len(datasets)} tiles into "
                    f"{merged_array.shape[2]}x{merged_array.shape[1]} px array"
                )
            finally:
                # Close all tile datasets — we have the merged array now
                for ds in datasets:
                    try:
                        ds.close()
                    except Exception:
                        pass

            # 4. Reproject to target CRS (EPSG:3006) and crop to bbox
            out_h, out_w = merged_array.shape[1], merged_array.shape[2]
            needs_reproject = (
                (target_width and target_height) or
                (src_crs != crs and src_crs != f"EPSG:{crs.split(':')[1] if ':' in crs else crs}")
            )

            if target_width and target_height:
                out_w, out_h = target_width, target_height

            if needs_reproject:
                from rasterio.enums import Resampling

                target_transform = from_bounds(
                    x_min, y_min, x_max, y_max, out_w, out_h
                )
                resampled = np.empty(
                    (merged_array.shape[0], out_h, out_w),
                    dtype=merged_array.dtype,
                )
                rasterio.warp.reproject(
                    merged_array,
                    resampled,
                    src_transform=merged_transform,
                    src_crs=src_crs,
                    dst_transform=target_transform,
                    dst_crs=crs,
                    resampling=Resampling.bilinear,
                )
                # Free the merged array before writing output
                del merged_array
                merged_array = resampled
                merged_transform = target_transform
                profile.update(crs=crs)

            # 5. Write merged GeoTIFF to bytes
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
                f"STAC Höjd: merged {len(tile_paths)} tile(s) "
                f"into {out_w}x{out_h} px GeoTIFF ({len(merged_bytes)} bytes)"
            )
            return merged_bytes

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching STAC elevation: {e.response.status_code}")
        if e.response.text:
            logger.error(f"Response: {e.response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Error fetching STAC elevation: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None
    finally:
        # Always clean up temp directory and all tile files
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.debug(f"Cleaned up temp tile directory: {tmp_dir}")
        except Exception:
            pass
