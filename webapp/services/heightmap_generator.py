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
from config.enfusion import snap_to_tile_multiple

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

    # Interpolate nodata values using nearest-neighbour.
    # Always check for NaN even when the GeoTIFF has no explicit nodata value
    # (e.g. some STAC providers don't set nodata in the profile but still
    # produce NaN for void/sea areas after reprojection).
    nodata_val = metadata["nodata"]
    if nodata_val is not None:
        nodata_mask = (elevation == nodata_val) | np.isnan(elevation)
    else:
        nodata_mask = np.isnan(elevation)

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

    # Safety check 1: detect implausible elevation ranges.
    # Real-world elevation spans from ~-430 m (Dead Sea) to ~8849 m (Everest).
    # For a typical Arma map selection (< 30 km), a range > 5000 m is almost
    # certainly corrupt — e.g. leaked nodata sentinel values (-9999) or
    # sea-floor bathymetry from coastal STAC tiles.
    elev_min = float(np.nanmin(elevation))
    elev_max = float(np.nanmax(elevation))
    elev_range = elev_max - elev_min
    if elev_range > 5000:
        msg = (
            f"DEM has implausible elevation range: {elev_min:.1f}m to "
            f"{elev_max:.1f}m (range: {elev_range:.0f}m). "
            f"This likely indicates corrupt nodata/sea values in the source data."
        )
        logger.error(msg)
        raise ElevationTruncatedError(msg)

    # Safety check 2: detect silently truncated responses where the WCS
    # server returned the correct image dimensions but only filled a
    # small corner with real data (rest is near-zero).  This happens
    # with some national WCS endpoints (e.g. Poland's geoportal) when
    # the requested area exceeds an undocumented size limit.
    #
    # Coastal/ocean selections are exempt: COP30 and similar global DEMs store
    # ocean pixels at exactly 0.0 m (sea level), not as nodata, so a coastal
    # area with 60-70% ocean coverage will legitimately hit the 50% threshold.
    # We distinguish truncation from ocean by requiring that the non-near-zero
    # (land) pixels are both numerous (≥10% of total) and show realistic
    # elevation variation (std > 0.5 m).  Truncated responses have almost no
    # valid data and/or zero variance; genuine coastal data has both.
    total_pixels = elevation.size
    near_zero_count = np.sum(np.abs(elevation) < 0.01)
    near_zero_pct = near_zero_count / total_pixels * 100
    if near_zero_pct > 50:
        non_zero = elevation[np.abs(elevation) >= 0.01]
        non_zero_pct = non_zero.size / total_pixels * 100
        land_std = float(np.std(non_zero)) if non_zero.size > 0 else 0.0
        if non_zero_pct >= 10 and land_std > 0.5:
            # Enough land pixels with realistic variation → coastal/ocean area.
            logger.warning(
                f"DEM has {near_zero_pct:.1f}% near-zero pixels, but "
                f"{non_zero_pct:.1f}% are valid land data (std={land_std:.1f}m). "
                f"Treating as coastal/ocean area, not truncated."
            )
        else:
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


_RIVER_WATER_TYPES = ("river", "stream", "canal", "ditch", "drain")


def _rasterize_river_mask(
    water_features: dict,
    width: int,
    height: int,
    bbox_wgs84: tuple[float, float, float, float],
    pixel_size_m: float,
) -> np.ndarray:
    """Rasterize OSM river/stream/canal LineStrings as a buffered band mask."""
    from services.feature_extractor import _estimate_river_width
    from services.utils.rasterize import rasterize_lines_per_feature_width

    def _half_width_px(feature: dict) -> int:
        water_type = (feature.get("properties", {}) or {}).get("water_type", "")
        width_m = _estimate_river_width(water_type)
        return max(1, int(round(width_m / (2.0 * pixel_size_m))))

    def _is_river(feature: dict) -> bool:
        water_type = (feature.get("properties", {}) or {}).get("water_type", "")
        return water_type in _RIVER_WATER_TYPES

    return rasterize_lines_per_feature_width(
        water_features,
        width,
        height,
        bbox_wgs84,
        buffer_px_fn=_half_width_px,
        filter_fn=_is_river,
    )


def _synthesize_sea_mask(
    water_features: dict,
    elevation: np.ndarray,
    bbox_wgs84: tuple[float, float, float, float],
    pixel_size_m: float,
    sea_level_threshold: float = 0.5,
    coast_proximity_px: int = 50,
) -> np.ndarray:
    """
    Build a sea polygon mask from OSM `natural=coastline` LineStrings + DEM.

    OSM ships the coast as a LineString (with the sea conventionally on the
    right), not as a polygon — there is no offshore feature we can rasterize
    directly. We synthesize one by flood-filling low-elevation pixels from
    bbox-edge seed pixels that are near the coastline. Returns an empty mask
    for inland maps (no `coastline` features) or maps with no low-elevation
    pixels at the bbox edge near a coast.
    """
    from services.utils.rasterize import rasterize_lines_per_feature_width

    height, width = elevation.shape

    def _is_coastline(feature: dict) -> bool:
        return (feature.get("properties", {}) or {}).get("water_type") == "coastline"

    coast = rasterize_lines_per_feature_width(
        water_features,
        width,
        height,
        bbox_wgs84,
        buffer_px_fn=lambda _f: 0,  # 1-px stroke (line_width = 2*0+1 = 1)
        filter_fn=_is_coastline,
    )
    if coast.sum() == 0:
        return np.zeros((height, width), dtype=np.uint8)

    low = elevation <= sea_level_threshold
    edge = np.zeros_like(low, dtype=bool)
    edge[0, :] = True
    edge[-1, :] = True
    edge[:, 0] = True
    edge[:, -1] = True

    near_coast = ndimage.binary_dilation(
        coast.astype(bool), iterations=coast_proximity_px,
    )
    seed = low & edge & near_coast
    if not seed.any():
        seed = low & edge
        if not seed.any():
            return np.zeros((height, width), dtype=np.uint8)

    sea = ndimage.binary_propagation(seed, mask=low)
    return sea.astype(np.uint8)


def flatten_water_in_heightmap(
    elevation: np.ndarray,
    water_mask: np.ndarray,
    transition_px: int = 5,
    pixel_size_m: float = 2.0,
    max_depth_m: float = 8.0,
    shore_slope_m_per_m: float = 0.3,
    region_depth_map: dict[int, float] | None = None,
) -> np.ndarray:
    """
    Set water-surface level per region and carve a depth bowl below it.

    For each connected water region we compute a single water-surface elevation
    (10th percentile of the region's source-DEM elevations — robust against
    DEM/OSM misalignment that leaves the odd peak inside a lake polygon). The
    Lake Generator prefab in Enfusion later draws the water surface at this
    level. The terrain inside the polygon is lowered with a *linear gradient*
    that runs from 0 at the shore to `region_max_depth` at the deepest
    interior point:

        region_max_depth = min(max_depth_m, max_dist_m × shore_slope_m_per_m)
        depth(pixel)     = region_max_depth × dist_to_shore(pixel) / max_dist

    Pre-v1.3.5 every pixel further than `max_depth_m / shore_slope_m_per_m`
    from any shore hit `max_depth_m` and the whole interior was a flat
    plateau. The new shape ramps continuously across the region, so the bowl
    is visible in the heightmap PNG for any region size, while small ponds
    still stay shallow (their `max_dist_m × slope` cap fires before
    `max_depth_m`). `region_depth_map`, if given, overrides `max_depth_m`
    per labelled region so different water types (lakes/rivers/sea) can be
    carved with different ceilings in a single call.
    """
    if water_mask.sum() == 0:
        return elevation

    result = elevation.copy()
    water_surface_field = elevation.copy()

    water_mask_bool = water_mask.astype(bool)
    labeled, n_features = ndimage.label(water_mask_bool)
    if n_features == 0:
        return elevation

    region_levels: dict[int, float] = {}
    for i in range(1, n_features + 1):
        region_mask = labeled == i
        region_elevations = elevation[region_mask]
        if region_elevations.size == 0:
            continue
        water_level = float(np.percentile(region_elevations, 10))
        region_levels[i] = water_level
        water_surface_field[region_mask] = water_level

    # Single global EDT, then scale per region by that region's maximum
    # shore distance so every region reaches its full `max_depth_m` at its
    # deepest interior point — small ponds get a shallow bowl, large lakes
    # a deep one, both with a continuous gradient.
    dist_px = ndimage.distance_transform_edt(water_mask_bool)
    region_ids = np.arange(1, n_features + 1)
    max_dist_per_region = ndimage.maximum(dist_px, labeled, index=region_ids)

    region_depth_map = region_depth_map or {}

    for region_id, water_level in region_levels.items():
        region_mask = labeled == region_id
        max_d_px = float(max_dist_per_region[region_id - 1])
        if max_d_px <= 0:
            continue
        ceiling = float(region_depth_map.get(region_id, max_depth_m))
        # Per-region cap: small ponds stay shallow (slope × radius caps depth
        # below max_depth_m), large lakes hit `ceiling` at their deepest
        # point. Either way the depth ramps linearly to that maximum.
        region_max_depth = min(
            ceiling,
            max_d_px * pixel_size_m * shore_slope_m_per_m,
        )
        norm = dist_px[region_mask] / max_d_px  # 0 at shore, 1 at deepest pt
        result[region_mask] = water_level - region_max_depth * norm

    # Shore blending: smooth land just outside water toward the water *surface*
    # level — not the carved bed — so the bowl doesn't bleed into the terrain.
    if transition_px > 0:
        dilated = ndimage.binary_dilation(
            water_mask_bool, iterations=transition_px,
        )
        transition_zone = dilated & ~water_mask_bool

        if transition_zone.any():
            blend = parallel_gaussian_filter(
                water_mask.astype(np.float32), sigma=transition_px,
            )
            blend = np.clip(blend, 0, 1)
            water_elev = parallel_gaussian_filter(
                water_surface_field, sigma=transition_px,
            )
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

    # 0. Normalize target_size to (px_x, px_z) and snap each axis.
    # target_size is the output heightmap dimension in pixels; an N-face
    # terrain needs an (N+1)-pixel heightmap. Snap the face count to a valid
    # tile multiple (×128), then add 1 back for the vertex/pixel count.
    if isinstance(target_size, int):
        target_size = (target_size, target_size)

    original_size = target_size
    size_x = snap_to_tile_multiple(target_size[0] - 1) + 1
    size_z = snap_to_tile_multiple(target_size[1] - 1) + 1
    target_size = (size_x, size_z)

    if target_size != original_size:
        logger.info(
            f"Snapped heightmap size from {original_size[0]}x{original_size[1]} "
            f"to {size_x}x{size_z} "
            f"(terrain grid size must be a multiple of 128 faces)"
        )
        if job:
            job.add_log(
                f"Adjusted heightmap size: {original_size[0]}x{original_size[1]} → "
                f"{size_x}x{size_z} (terrain grid size must be a multiple of 128 faces)"
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

    # 4. Level water bodies — four passes, one per water type, so each
    # gets its own depth ceiling and rivers don't get merged into adjacent
    # lakes by the connected-component labelling.
    if water_features and water_features.get("features"):
        logger.info("Leveling water bodies...")
        if job:
            job.add_log(f"Leveling {len(water_features['features'])} water bodies...")
            job.progress = 53
        bbox = metadata.get("bounds")
        if bbox:
            bbox_tuple = (bbox.left, bbox.bottom, bbox.right, bbox.top)
            arr_h, arr_w = elevation.shape

            lake_mask = rasterize_features_to_mask(
                water_features, arr_w, arr_h, bbox_tuple,
                filter_tags={
                    "natural": ["water"],
                    "water_type": ["lake", "pond", "reservoir", "water", "basin"],
                },
            )
            river_mask = _rasterize_river_mask(
                water_features, arr_w, arr_h, bbox_tuple, target_resolution_m,
            )
            wetland_mask = rasterize_features_to_mask(
                water_features, arr_w, arr_h, bbox_tuple,
                filter_tags={"water_type": ["wetland"]},
            )
            sea_mask = _synthesize_sea_mask(
                water_features, elevation, bbox_tuple, target_resolution_m,
            )

            # Carve each type with its own depth ceiling. Order matters only
            # where masks overlap (a river crossing a lake gets overwritten
            # by the lake pass — desirable).
            for mask, max_depth, label in (
                (river_mask, 2.0, "river/stream"),
                (wetland_mask, 1.0, "wetland"),
                (sea_mask, 30.0, "sea"),
                (lake_mask, 8.0, "lake/pond/reservoir"),
            ):
                if mask.sum() == 0:
                    continue
                logger.info(
                    f"Carving bathymetry for {label} mask: "
                    f"{int(mask.sum())} px, max_depth={max_depth} m"
                )
                elevation = flatten_water_in_heightmap(
                    elevation,
                    mask,
                    transition_px=3,
                    pixel_size_m=target_resolution_m,
                    max_depth_m=max_depth,
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
        # dimensions = heightmap pixels (N+1); terrain_grid_size = faces (N);
        # terrain_size_m = faces × cell (N×C)
        "dimensions": f"{heightmap.shape[1]}x{heightmap.shape[0]}",
        "terrain_grid_size": f"{heightmap.shape[1] - 1}x{heightmap.shape[0] - 1}",
        "terrain_size_m": f"{(heightmap.shape[1] - 1) * target_resolution_m:.0f}x{(heightmap.shape[0] - 1) * target_resolution_m:.0f}",
        "grid_cell_size_m": target_resolution_m,
        # Intermediate arrays for downstream steps (e.g. surface mask generation)
        # so callers don't need to re-parse the DEM from raw bytes.
        "_elevation_array": elevation,
        "_dem_metadata": metadata,
        **height_info,
    }
