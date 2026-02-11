"""
Heightmap generation, refinement, and export.

Merges:
- generate_heightmap_from_array() from app/services/heightmap.py (cleaner API)
- flatten_roads_in_heightmap() from app/ (uses ndimage properly)
- flatten_water_in_heightmap() from app/ (labels connected water regions)
- nodata interpolation from webapp/ (NearestNDInterpolator)
- 8-bit preview generation from webapp/
- save_heightmap_png/asc/metadata from app/
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.interpolate import NearestNDInterpolator

# Rasterization utilities live in the shared utils module.
# Re-exported here for backward compatibility (surface_mask_generator imports from here).
from services.utils.rasterize import rasterize_features_to_mask  # noqa: F401
from services.utils.parallel import parallel_gaussian_filter, parallel_zoom
from config.enfusion import snap_to_enfusion_size, VALID_ENFUSION_VERTEX_COUNTS

logger = logging.getLogger(__name__)


class ElevationTruncatedError(Exception):
    """Raised when the WCS elevation data appears silently truncated.

    Some WCS endpoints (e.g. Poland's geoportal) return valid TIFF files
    with correct dimensions but only partial elevation data.  The unfilled
    portion is near-zero, producing broken heightmaps.

    When this is raised the caller should fall back to a lower-resolution
    global source (e.g. OpenTopography Copernicus DEM 30 m).
    """


# ---------------------------------------------------------------------------
# GeoTIFF to array with nodata interpolation
# ---------------------------------------------------------------------------

def geotiff_to_array(geotiff_bytes: bytes) -> tuple[np.ndarray, dict]:
    """
    Convert GeoTIFF bytes to numpy array and metadata.
    Handles nodata values by interpolating from nearest valid neighbours.

    Returns:
        (elevation_array, metadata_dict)
    """
    import rasterio
    from rasterio.io import MemoryFile

    # Validate input data
    if not geotiff_bytes:
        raise ValueError("Empty GeoTIFF data provided")

    if len(geotiff_bytes) < 8:
        raise ValueError(f"GeoTIFF data too small ({len(geotiff_bytes)} bytes)")

    # Check for valid TIFF magic bytes
    if geotiff_bytes[:4] not in (b"II*\x00", b"MM\x00*"):
        first_bytes = geotiff_bytes[:8].hex()
        logger.error(f"Invalid TIFF magic bytes. First 8 bytes: {first_bytes}")

        # Check if this is an XML error response
        if geotiff_bytes.startswith(b"<?xml") or geotiff_bytes.startswith(b"<"):
            try:
                xml_preview = geotiff_bytes[:500].decode('utf-8', errors='replace')
                logger.error(f"Response appears to be XML (likely a WCS error): {xml_preview}")
                raise ValueError(
                    f"Elevation service returned an XML error instead of a GeoTIFF. "
                    f"This usually indicates invalid credentials, incorrect parameters, or service unavailability. "
                    f"Check the logs for the full error message."
                )
            except:
                pass

        raise ValueError(
            f"Data does not appear to be a valid TIFF file. "
            f"Expected TIFF magic bytes (II*\\x00 or MM\\x00*), "
            f"got: {first_bytes}"
        )

    logger.debug(f"Opening GeoTIFF from memory ({len(geotiff_bytes)} bytes)")

    try:
        with MemoryFile(geotiff_bytes) as memfile:
            with memfile.open() as dataset:
                elevation = dataset.read(1).astype(np.float32)
                metadata = {
                    "crs": str(dataset.crs),
                    "transform": dataset.transform,
                    "width": dataset.width,
                    "height": dataset.height,
                    "bounds": dataset.bounds,
                    "nodata": dataset.nodata,
                    "resolution": dataset.res,
                }
    except Exception as e:
        logger.error(f"Failed to read GeoTIFF: {e}")
        logger.error(f"Data size: {len(geotiff_bytes)} bytes, first 100 bytes: {geotiff_bytes[:100].hex()}")
        raise

    # Interpolate nodata values using nearest-neighbour
    if metadata["nodata"] is not None:
        nodata_mask = (elevation == metadata["nodata"]) | np.isnan(elevation)
        if np.any(nodata_mask):
            valid = ~nodata_mask
            if np.any(valid):
                rows, cols = np.where(valid)
                values = elevation[valid]
                interp = NearestNDInterpolator(list(zip(rows, cols)), values)
                nodata_rows, nodata_cols = np.where(nodata_mask)
                if len(nodata_rows) > 0:
                    elevation[nodata_mask] = interp(nodata_rows, nodata_cols)
                    logger.info(f"Interpolated {len(nodata_rows)} nodata pixels")

    # Safety check: detect silently truncated responses where the WCS
    # server returned the correct image dimensions but only filled a
    # small corner with real data (rest is near-zero).  This happens
    # with some national WCS endpoints (e.g. Poland's geoportal) when
    # the requested area exceeds an undocumented size limit.
    total_pixels = elevation.size
    near_zero_count = np.sum(np.abs(elevation) < 0.01)
    near_zero_pct = near_zero_count / total_pixels * 100
    if near_zero_pct > 50:
        msg = (
            f"DEM appears truncated: {near_zero_pct:.1f}% of pixels are near-zero "
            f"({near_zero_count}/{total_pixels}). The elevation API silently "
            f"returned incomplete data."
        )
        logger.error(msg)
        raise ElevationTruncatedError(msg)

    logger.info(
        f"DEM: {elevation.shape}, "
        f"range: {np.nanmin(elevation):.1f} - {np.nanmax(elevation):.1f} m"
    )
    return elevation, metadata


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def resample_dem(
    elevation: np.ndarray,
    metadata: dict,
    target_resolution_m: float,
    target_size: Optional[int | tuple[int, int]] = None,
) -> tuple[np.ndarray, dict]:
    """
    Resample DEM to target resolution or exact pixel dimensions.

    Args:
        elevation: Input elevation array
        metadata: DEM metadata with resolution info
        target_resolution_m: Target resolution in metres
        target_size: If set, resize to these dimensions.
            - int: square (size x size)
            - tuple (size_x, size_z): non-square (width x height)

    Returns:
        (resampled_elevation, updated_metadata)
    """
    if target_size:
        if isinstance(target_size, tuple):
            size_x, size_z = target_size
        else:
            size_x = size_z = target_size
        zoom_y = size_z / elevation.shape[0]
        zoom_x = size_x / elevation.shape[1]
        elevation = parallel_zoom(elevation, (zoom_y, zoom_x), order=3)
        metadata["width"] = size_x
        metadata["height"] = size_z
    else:
        current_res = metadata.get("resolution", (30, 30))
        if isinstance(current_res, tuple):
            current_res_m = abs(current_res[0])
        else:
            current_res_m = abs(current_res)

        if current_res_m > 0 and target_resolution_m > 0:
            zoom_factor = current_res_m / target_resolution_m
            if abs(zoom_factor - 1.0) > 0.01:
                elevation = parallel_zoom(elevation, zoom_factor, order=3)
                metadata["width"] = elevation.shape[1]
                metadata["height"] = elevation.shape[0]
                metadata["resolution"] = (target_resolution_m, target_resolution_m)

    logger.info(f"Resampled DEM to {elevation.shape}")
    return elevation, metadata


# ---------------------------------------------------------------------------
# Core heightmap conversion (from app/services/heightmap.py)
# ---------------------------------------------------------------------------

def generate_heightmap_from_array(
    elevation: np.ndarray,
    nodata: float | None = None,
) -> tuple[np.ndarray, dict]:
    """
    Convert a float elevation array to a 16-bit heightmap.

    Returns:
        (uint16_array, metadata) where metadata contains min/max/scale info.
    """
    valid_mask = np.ones_like(elevation, dtype=bool)
    if nodata is not None:
        valid_mask = ~np.isnan(elevation) & (elevation != nodata)

    valid_data = elevation[valid_mask]
    if valid_data.size == 0:
        return np.zeros_like(elevation, dtype=np.uint16), {
            "min_elevation": 0, "max_elevation": 0,
            "elevation_range": 0, "height_scale": 0, "height_offset": 0,
            "width": elevation.shape[1], "height": elevation.shape[0],
        }

    min_elev = float(np.min(valid_data))
    max_elev = float(np.max(valid_data))
    elev_range = max(max_elev - min_elev, 0.01)

    normalized = np.zeros_like(elevation, dtype=np.float32)
    normalized[valid_mask] = (elevation[valid_mask] - min_elev) / elev_range * 65535.0
    normalized[~valid_mask] = 0
    heightmap = np.clip(normalized, 0, 65535).astype(np.uint16)

    height_scale = elev_range / 65535.0

    metadata = {
        "min_elevation": min_elev,
        "max_elevation": max_elev,
        "elevation_range": elev_range,
        "height_scale": height_scale,
        "height_offset": min_elev,
        "width": heightmap.shape[1],
        "height": heightmap.shape[0],
    }
    return heightmap, metadata


# ---------------------------------------------------------------------------
# Heightmap refinement (from app/services/heightmap.py)
# ---------------------------------------------------------------------------

def flatten_roads_in_heightmap(
    elevation: np.ndarray,
    road_mask: np.ndarray,
    road_width_px: int = 5,
    smooth_radius: int = 10,
) -> np.ndarray:
    """
    Flatten terrain under roads and smooth transitions.

    Uses ndimage for efficient morphological operations:
    - Dilate road mask to cover road width
    - Gaussian-smooth the elevation
    - Blend smoothed elevation into the road corridor
    """
    if road_mask.sum() == 0:
        return elevation

    result = elevation.copy()

    struct = ndimage.generate_binary_structure(2, 1)
    dilated = ndimage.binary_dilation(road_mask, struct, iterations=road_width_px)

    # Smooth road elevation (multi-threaded)
    road_smooth = parallel_gaussian_filter(elevation, sigma=smooth_radius)

    # Blend: road areas get smoothed elevation, transition zone blends
    blend_mask = parallel_gaussian_filter(dilated.astype(np.float32), sigma=smooth_radius)
    blend_mask = np.clip(blend_mask, 0, 1)

    result = elevation * (1 - blend_mask) + road_smooth * blend_mask
    return result


def flatten_water_in_heightmap(
    elevation: np.ndarray,
    water_mask: np.ndarray,
    transition_px: int = 5,
) -> np.ndarray:
    """
    Flatten lake surfaces and carve river beds.

    Labels connected water regions and sets each to its 10th percentile
    elevation (water flows to lowest point). Smooths shoreline transitions.
    """
    if water_mask.sum() == 0:
        return elevation

    result = elevation.copy()

    labeled, n_features = ndimage.label(water_mask)

    for i in range(1, n_features + 1):
        region_mask = labeled == i
        region_elevations = elevation[region_mask]
        if region_elevations.size == 0:
            continue
        water_level = np.percentile(region_elevations, 10)
        result[region_mask] = water_level

    if transition_px > 0:
        dilated = ndimage.binary_dilation(
            water_mask.astype(bool), iterations=transition_px,
        )
        transition_zone = dilated & ~water_mask.astype(bool)

        if transition_zone.any():
            blend = parallel_gaussian_filter(
                water_mask.astype(np.float32), sigma=transition_px,
            )
            blend = np.clip(blend, 0, 1)
            water_elev = parallel_gaussian_filter(result, sigma=transition_px)
            result = np.where(
                transition_zone,
                elevation * (1 - blend) + water_elev * blend,
                result,
            )

    return result


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------

def save_heightmap_png(heightmap: np.ndarray, output_path: str) -> str:
    """Save a 16-bit heightmap as PNG."""
    img = Image.fromarray(heightmap, mode="I;16")
    img.save(output_path)
    logger.info(f"Saved heightmap PNG: {output_path} ({heightmap.shape[1]}x{heightmap.shape[0]})")
    return output_path


def save_heightmap_preview(heightmap: np.ndarray, output_path: str) -> str:
    """Save an 8-bit grayscale preview of the heightmap."""
    try:
        preview = (heightmap.astype(np.float32) / 256).astype(np.uint8)
        img = Image.fromarray(preview, mode="L")
        img.save(output_path)
        logger.info(f"Saved heightmap preview: {output_path}")
        return output_path
    except Exception as e:
        logger.error(f"Failed to save heightmap preview to {output_path}: {e}", exc_info=True)
        raise


def save_heightmap_asc(
    heightmap: np.ndarray,
    output_path: str,
    cellsize: float = 2.0,
    xllcorner: float = 0.0,
    yllcorner: float = 0.0,
    nodata_value: int = -9999,
    height_scale: float = 1.0,
    height_offset: float = 0.0,
) -> str:
    """
    Save heightmap as ESRI ASCII Grid (.asc) for Enfusion import.

    The ASC contains real elevation values (not 16-bit normalised).
    """
    nrows, ncols = heightmap.shape
    real_elevation = heightmap.astype(np.float32) * height_scale + height_offset

    with open(output_path, "w") as f:
        f.write(f"ncols         {ncols}\n")
        f.write(f"nrows         {nrows}\n")
        f.write(f"xllcorner     {xllcorner}\n")
        f.write(f"yllcorner     {yllcorner}\n")
        f.write(f"cellsize      {cellsize}\n")
        f.write(f"NODATA_value  {nodata_value}\n")
        # Vectorized export: numpy formats the entire grid in C, ~10-50× faster
        # than the Python row loop it replaces.
        np.savetxt(f, real_elevation, fmt="%.3f", delimiter=" ")

    logger.info(f"Saved heightmap ASC: {output_path} ({ncols}x{nrows})")
    return output_path


def save_metadata(metadata: dict, output_path: str) -> str:
    """Save terrain metadata as JSON."""
    with open(output_path, "w") as f:
        json.dump(metadata, f, indent=2, default=str)
    return output_path


# ---------------------------------------------------------------------------
# Main heightmap pipeline (composite)
# ---------------------------------------------------------------------------

def generate_heightmap(
    dem_bytes: bytes,
    road_features: Optional[dict] = None,
    water_features: Optional[dict] = None,
    target_size: int | tuple[int, int] = 4096,
    target_resolution_m: float = 2.0,
    output_dir: Optional[Path] = None,
    job = None,
) -> dict:
    """
    Main heightmap generation pipeline.

    Args:
        dem_bytes: Raw GeoTIFF DEM data
        road_features: GeoJSON roads for flattening
        water_features: GeoJSON water bodies for leveling
        target_size: Output heightmap dimensions (pixels).
            - int: square heightmap (size x size)
            - tuple (size_x, size_z): non-square heightmap (width x height)
        target_resolution_m: Target resolution in metres
        output_dir: Directory for output files
        job: Optional MapGenerationJob for logging

    Returns:
        Dict with heightmap data and metadata
    """
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp())

    # 0. Normalize target_size to (size_x, size_z) tuple and snap each axis
    if isinstance(target_size, int):
        target_size = (target_size, target_size)

    original_size = target_size
    size_x = snap_to_enfusion_size(target_size[0])
    size_z = snap_to_enfusion_size(target_size[1])
    target_size = (size_x, size_z)

    if target_size != original_size:
        logger.info(
            f"Snapped heightmap size from {original_size[0]}x{original_size[1]} "
            f"to {size_x}x{size_z} "
            f"(Enfusion requires power-of-2 faces per axis)"
        )
        if job:
            job.add_log(
                f"Adjusted heightmap size: {original_size[0]}x{original_size[1]} → "
                f"{size_x}x{size_z} (Enfusion requires power-of-2 faces per axis)"
            )

    # 1. Parse GeoTIFF
    logger.info("Parsing DEM data...")
    if job:
        job.add_log(f"Validating elevation data ({len(dem_bytes) / 1024 / 1024:.1f} MB)...")
        job.progress = 42
    elevation, metadata = geotiff_to_array(dem_bytes)

    # Free the raw GeoTIFF bytes now that we have the numpy array.
    # For large Sweden STAC tiles this can be ~192 MB.
    del dem_bytes
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # Not on glibc (e.g. musl/macOS) — skip

    if job:
        job.add_log(
            f"Elevation parsed: {elevation.shape[1]}×{elevation.shape[0]} pixels, "
            f"range {np.nanmin(elevation):.1f}m – {np.nanmax(elevation):.1f}m"
        )

    # 2. Resample
    logger.info(f"Resampling to {size_x}x{size_z}...")
    if job:
        job.add_log(f"Resampling elevation data to {size_x}x{size_z} pixels...")
        job.progress = 45
    elevation, metadata = resample_dem(elevation, metadata, target_resolution_m, target_size)

    # 3. Flatten roads
    if road_features and road_features.get("features"):
        logger.info("Flattening terrain along roads...")
        if job:
            job.add_log(f"Flattening terrain along {len(road_features['features'])} road segments...")
            job.progress = 50
        bbox = metadata.get("bounds")
        if bbox:
            road_mask = rasterize_features_to_mask(
                road_features,
                elevation.shape[1], elevation.shape[0],
                (bbox.left, bbox.bottom, bbox.right, bbox.top),
                buffer_px=2,
            )
            elevation = flatten_roads_in_heightmap(
                elevation, road_mask, road_width_px=3, smooth_radius=5,
            )

    # 4. Level water bodies
    if water_features and water_features.get("features"):
        logger.info("Leveling water bodies...")
        if job:
            job.add_log(f"Leveling {len(water_features['features'])} water bodies...")
            job.progress = 53
        bbox = metadata.get("bounds")
        if bbox:
            water_mask = rasterize_features_to_mask(
                water_features,
                elevation.shape[1], elevation.shape[0],
                (bbox.left, bbox.bottom, bbox.right, bbox.top),
                filter_tags={"natural": ["water"], "water_type": ["lake", "pond", "reservoir"]},
            )
            elevation = flatten_water_in_heightmap(
                elevation, water_mask, transition_px=3,
            )

    # 5. Light smoothing pass
    logger.info("Applying final smoothing...")
    if job:
        job.add_log("Applying final terrain smoothing...")
        job.progress = 55
    elevation = parallel_gaussian_filter(elevation, sigma=0.5)

    # 6. Normalise to 16-bit
    if job:
        job.progress = 56
    heightmap, height_info = generate_heightmap_from_array(elevation)

    # 7. Export
    if job:
        job.add_log("Saving heightmap files...")
        job.progress = 57
    png_path = save_heightmap_png(heightmap, str(output_dir / "heightmap.png"))
    asc_path = save_heightmap_asc(
        heightmap, str(output_dir / "heightmap.asc"),
        cellsize=target_resolution_m,
        height_scale=height_info["height_scale"],
        height_offset=height_info["height_offset"],
    )
    preview_path = save_heightmap_preview(heightmap, str(output_dir / "heightmap_preview.png"))

    return {
        "heightmap_png": png_path,
        "heightmap_asc": asc_path,
        "heightmap_preview": preview_path,
        "dimensions": f"{heightmap.shape[1]}x{heightmap.shape[0]}",
        "terrain_size_m": f"{heightmap.shape[1] * target_resolution_m:.0f}x{heightmap.shape[0] * target_resolution_m:.0f}",
        "grid_cell_size_m": target_resolution_m,
        # Intermediate arrays for downstream steps (e.g. surface mask generation)
        # so callers don't need to re-parse the DEM from raw bytes.
        "_elevation_array": elevation,
        "_dem_metadata": metadata,
        **height_info,
    }
