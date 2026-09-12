"""
Heightmap generation, refinement, and export.

Merges:
- generate_heightmap_from_array() from app/services/heightmap.py (cleaner API)
- flatten_roads_in_heightmap() from app/ (uses ndimage properly)
- flatten_water_in_heightmap() from app/ (labels connected water regions)
- nodata interpolation from webapp/ (nearest-neighbour via EDT indices)
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

# Rasterization utilities live in the shared utils module.
# Re-exported here for backward compatibility (surface_mask_generator imports from here).
from services.utils.rasterize import rasterize_features_to_mask  # noqa: F401
from services.utils.parallel import (
    parallel_edt,
    parallel_gaussian_filter,
    parallel_zoom,
)
from config.enfusion import snap_to_tile_multiple, pick_clean_height_scale
from config.lakes import (
    LAKE_MAX_DEPTH_M,
    LAKE_SHORE_SLOPE_M_PER_M,
    SEA_DROPOFF_SLOPE_M_PER_M,
    SEA_FULL_DEPTH_DISTANCE_M,
    SEA_MAX_DEPTH_M,
    SEA_MIN_AREA_FRACTION,
    SEA_MIN_DEPTH_M,
    SEA_MUST_TOUCH_MAP_EDGE,
    SEA_SHELF_DEPTH_M,
    SEA_SHELF_WIDTH_M,
)

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

    n_nodata = int(np.count_nonzero(nodata_mask))
    if n_nodata and n_nodata < nodata_mask.size:
        # Exact nearest-neighbour fill via the EDT's index output.
        # The previous NearestNDInterpolator built a Python list of one tuple
        # per *valid* pixel and a KD-tree over all of them — ~25 million points
        # on a 4993x4993 DEM, minutes of work and gigabytes of RAM to patch a
        # handful of voids. The EDT gives the same nearest source pixel for
        # every hole (verified: identical distances, only equidistant ties
        # break differently) at a cost that depends on the raster, not on how
        # many valid pixels surround it.
        nearest = ndimage.distance_transform_edt(
            nodata_mask, return_distances=False, return_indices=True,
        )
        elevation[nodata_mask] = elevation[
            nearest[0][nodata_mask], nearest[1][nodata_mask]
        ]
        del nearest
        logger.info(f"Interpolated {n_nodata} nodata pixels")

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
    # We distinguish truncation from ocean by the *quality* of the land pixels,
    # not how many there are: realistic elevation variation (std > 0.5 m) and a
    # summit a couple of metres above sea level. Truncated responses have
    # almost no valid data and/or zero variance.
    #
    # The land-fraction floor used to be 10%, which rejected small islands
    # outright: a 3.6 km selection around Isla Grosa (ES) is 97.4% ocean and
    # 2.6% island, and generation aborted with "select an area with sufficient
    # land coverage". That was defensible when island maps were not really
    # supported; v1.16.0 ships sea bathymetry specifically for them, so an
    # island has to be a legal selection. The floor is now 0.5% — enough to
    # still catch a response with essentially no data — and the variation and
    # summit checks do the real discrimination.
    total_pixels = elevation.size
    near_zero_count = np.sum(np.abs(elevation) < 0.01)
    near_zero_pct = near_zero_count / total_pixels * 100
    if near_zero_pct > 50:
        non_zero = elevation[np.abs(elevation) >= 0.01]
        non_zero_pct = non_zero.size / total_pixels * 100
        land_std = float(np.std(non_zero)) if non_zero.size > 0 else 0.0
        land_max = float(np.max(non_zero)) if non_zero.size > 0 else 0.0
        if non_zero_pct >= 0.5 and land_std > 0.5 and land_max > 2.0:
            # Land pixels look like real terrain → coastal/island area.
            logger.warning(
                f"DEM has {near_zero_pct:.1f}% near-zero pixels, but "
                f"{non_zero_pct:.1f}% are valid land data (std={land_std:.1f}m, "
                f"max={land_max:.1f}m). Treating as coastal/island area, not "
                f"truncated."
            )
        else:
            msg = (
                f"DEM appears truncated: {near_zero_pct:.1f}% of pixels are "
                f"near-zero ({near_zero_count}/{total_pixels}), and the "
                f"remaining {non_zero_pct:.2f}% do not look like land "
                f"(std={land_std:.2f} m, max={land_max:.1f} m). The elevation "
                f"API silently returned incomplete data."
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

def compute_land_datum(
    elevation: np.ndarray,
    water_mask: np.ndarray | None = None,
) -> float:
    """
    The absolute elevation, in metres, that should become world **Y = 0** in the
    World Editor — the lowest *land* point (issue #165).

    Before this existed, heightmaps were exported as absolute metres above sea
    level. That is correct for a coastal map, where the lowest land already sits
    at ~0 m and meets the engine's ocean plane. It is wrong for an inland map:
    Frösön sits in Storsjön at ~292 m, so the whole terrain imported 292 m above
    the ocean plane and floated in the sky.

    Carved lake and sea beds are excluded, otherwise the datum would be the
    bottom of the deepest water body and every piece of land would still float —
    made worse by v1.8.0, which deepened lakes from 8 m to 15 m.

    Returns the global minimum when there is no land at all (an all-water tile),
    which reproduces the previous behaviour rather than failing.

    The mask is coerced to bool before inversion. Every rasterizer in
    `services/utils/rasterize.py` returns uint8 0/1, and `~uint8` is 254/255 —
    integer *values*, not a boolean mask — so `elevation[~mask]` silently
    became fancy indexing and tried to allocate an (N, N, N) array (issue #183).
    """
    if water_mask is not None and water_mask.shape == elevation.shape and water_mask.any():
        land = elevation[~water_mask.astype(bool)]
        if land.size:
            return float(np.min(land))
        logger.warning(
            "Land datum: every pixel is water — falling back to the global "
            "minimum, so the terrain will sit at the deepest point"
        )
    return float(np.min(elevation))


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
            "dialog_height_scale": pick_clean_height_scale(0.0, 0.0),
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
        # Encoding scale: round-trips the normalised 16-bit grid back to absolute
        # metres when writing the .asc (do NOT show this to the user — #142).
        "height_scale": height_scale,
        "height_offset": min_elev,
        # Dialog scale: the clean value the user types into the "New Terrain"
        # dialog. Defaults to the engine default (0.03125) per Atlas 2 (#142).
        "dialog_height_scale": pick_clean_height_scale(min_elev, max_elev),
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


def dem_bbox_wgs84(metadata: dict) -> tuple[float, float, float, float] | None:
    """The DEM's bounds as a WGS84 (west, south, east, north) tuple.

    Every rasterizer in ``services/utils/rasterize.py`` maps a GeoJSON
    coordinate to a pixel with ``(lng - west) / lng_range * width`` — it assumes
    degrees. The DEM's own bounds are in the DEM's own CRS, and only some
    providers hand us WGS84:

    * COP30 / OpenTopography deliver EPSG:4326, so bounds are already degrees.
    * **Lantmäteriet STAC Höjd delivers EPSG:5845** (SWEREF99 TM + RH2000) and
      the merge keeps that CRS, so bounds are metres — around 742354, 6470557
      for Gotska Sandön.

    Feeding those metres in as degrees put every water and road feature far
    outside the raster, where ``_coords_to_pixels`` clamped it into the corner.
    On the Gotska Sandön run the lake mask came out as **1 pixel** and the sea
    mask as zero, so no bathymetry was carved and the island was written as an
    inland map — and road flattening had silently done nothing on every Swedish
    map for as long as the Lantmäteriet path has existed.

    Returns None when the DEM carries no bounds at all.
    """
    bounds = metadata.get("bounds")
    if not bounds:
        return None
    box = (
        float(bounds.left), float(bounds.bottom),
        float(bounds.right), float(bounds.top),
    )
    crs = str(metadata.get("crs") or "").strip()
    if not crs:
        logger.warning(
            "DEM has bounds but no CRS; assuming they are already WGS84. "
            "If features land in the corner of the map, this is why."
        )
        return box
    if "4326" in crs or crs.upper() in ("EPSG:4326", "WGS84"):
        return box
    try:
        from rasterio.warp import transform_bounds

        converted = transform_bounds(crs, "EPSG:4326", *box)
        logger.info(
            f"DEM bounds converted {crs} -> EPSG:4326 for feature "
            f"rasterisation: {box[0]:.1f},{box[1]:.1f},{box[2]:.1f},{box[3]:.1f}"
            f" -> {converted[0]:.4f},{converted[1]:.4f},"
            f"{converted[2]:.4f},{converted[3]:.4f}"
        )
        return converted
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            f"Could not convert DEM bounds from {crs} to WGS84 ({exc}); "
            f"using them unchanged. Water and road masks may be misplaced."
        )
        return box


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

    The coast arrives in two different shapes depending on the provider, and
    both have to work:

    * **OSM** ships it as a `natural=coastline` **LineString** (sea on the
      right), with no offshore feature to rasterize. We synthesize one by
      flood-filling low-elevation pixels from bbox-edge seeds near the line.
    * **Lantmäteriet Marktäcke** ships the sea itself as a **Polygon**
      (objekttyp 2631 → `water_type: "coastline"`). That is already the
      offshore area, so it is rasterized directly.

    Handling only the LineString form is why the sea was never detected on any
    Swedish map: an island in the Gulf of Bothnia came through with a single
    Polygon coastline feature, `rasterize_lines_per_feature_width` skipped it
    for not being a line, and the mask came back empty — so no bathymetry was
    carved and the map was treated as inland (issue #193 follow-up).

    Returns an empty mask for genuinely inland maps (no `coastline` features at
    all), which leaves the sea path a no-op.
    """
    from services.utils.rasterize import (
        rasterize_features_to_mask,
        rasterize_lines_per_feature_width,
    )

    height, width = elevation.shape

    def _is_coastline(feature: dict) -> bool:
        return (feature.get("properties", {}) or {}).get("water_type") == "coastline"

    # Polygon form: this *is* the sea, no synthesis needed.
    #
    # Restricted to Polygon/MultiPolygon on purpose. rasterize_features_to_mask
    # also strokes LineStrings, so filtering on the tag alone would draw the OSM
    # coastline *line* into the mask as a 1 px stripe — which lands on the
    # landward side of the coast as well and leaks sea into the highlands.
    polygon_only = {
        "type": "FeatureCollection",
        "features": [
            f for f in (water_features or {}).get("features", [])
            if _is_coastline(f)
            and (f.get("geometry") or {}).get("type")
            in ("Polygon", "MultiPolygon")
        ],
    }
    sea_polygons = (
        rasterize_features_to_mask(
            polygon_only, width, height, bbox_wgs84,
            filter_tags={"water_type": ["coastline"]},
        ).astype(bool)
        if polygon_only["features"]
        else np.zeros((height, width), dtype=bool)
    )

    coast = rasterize_lines_per_feature_width(
        water_features,
        width,
        height,
        bbox_wgs84,
        buffer_px_fn=lambda _f: 0,  # 1-px stroke (line_width = 2*0+1 = 1)
        filter_fn=_is_coastline,
    )
    if coast.sum() == 0:
        # No line to flood-fill from; the polygon form is all we have (and on a
        # Marktäcke map it is all we need).
        return sea_polygons.astype(np.uint8)

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
            return sea_polygons.astype(np.uint8)

    sea = ndimage.binary_propagation(seed, mask=low)
    return (sea | sea_polygons).astype(np.uint8)


def qualifying_sea_regions(
    sea_mask: np.ndarray,
    min_area_fraction: float = SEA_MIN_AREA_FRACTION,
    must_touch_edge: bool = SEA_MUST_TOUCH_MAP_EDGE,
) -> np.ndarray:
    """Keep only the parts of ``sea_mask`` that are genuinely open sea.

    Issue #193 asks for ocean bathymetry "only where there clearly is a larger
    body of water on the map. For all inland maps that do not have a shoreline
    (i.e. not an island) this should not apply."

    Two independent guards, both of which must hold:

    * **Area** — the region covers at least ``min_area_fraction`` of the map.
    * **Touches the map edge** — a sea continues past the selection. A water
      body that fits entirely inside the map is a lake however large it is, and
      carving a 100 m trench into a Swedish inland lake would be far worse than
      leaving it to the lake path.

    Returns a uint8 mask of the surviving regions; all-zero when none qualify,
    which is the inland case and leaves the sea path a no-op.
    """
    mask = sea_mask.astype(bool)
    if not mask.any():
        return np.zeros(mask.shape, dtype=np.uint8)

    labels, n = ndimage.label(mask)
    if n == 0:
        return np.zeros(mask.shape, dtype=np.uint8)

    total_px = mask.size
    keep = np.zeros(n + 1, dtype=bool)
    edge_ids = set()
    if must_touch_edge:
        for strip in (labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]):
            edge_ids.update(int(v) for v in np.unique(strip) if v)

    sizes = ndimage.sum(mask, labels, index=range(1, n + 1))
    for idx in range(1, n + 1):
        area_ok = (sizes[idx - 1] / total_px) >= min_area_fraction
        edge_ok = (not must_touch_edge) or (idx in edge_ids)
        keep[idx] = bool(area_ok and edge_ok)

    return keep[labels].astype(np.uint8)


def carve_sea_bathymetry(
    elevation: np.ndarray,
    sea_mask: np.ndarray,
    pixel_size_m: float,
    sea_surface_m: float = 0.0,
) -> np.ndarray:
    """Carve a shelf-then-dropoff sea floor under ``sea_mask`` (issue #193).

    A sea is not a big lake. ``flatten_water_in_heightmap`` gives a lake one
    linear ramp from the shore, which on a coast produces a uniformly shallow
    dish. A real coast has a shelf you can wade and swim off, then a drop to
    depth the player never reaches:

    ```
    depth(d) = SEA_SHELF_DEPTH_M * d / SEA_SHELF_WIDTH_M            d <= shelf
               SEA_SHELF_DEPTH_M + (d - shelf) * dropoff_slope      d >  shelf
    ```

    capped per region at a ceiling interpolated between ``SEA_MIN_DEPTH_M`` and
    ``SEA_MAX_DEPTH_M`` by how far offshore that region actually reaches — the
    "dynamically to 30-100 metres" in the issue. A narrow strait does not get an
    abyss; open ocean is not capped at wading depth.

    The water *surface* is set to ``sea_surface_m`` (absolute metres, 0.0 = mean
    sea level) rather than to the lowest surrounding land, so the floor is
    genuinely below sea level and the engine's ocean plane at Y=0 sits on it.

    Returns a new array; ``elevation`` is not modified.
    """
    mask = sea_mask.astype(bool)
    if not mask.any():
        return elevation

    result = elevation.copy().astype(np.float32)

    # Distance from the nearest non-sea pixel, growing offshore. Pad with the
    # edge value so the map border is not treated as a shore: the sea continues
    # past the selection, and without this every coastal map would shoal back
    # to 0 m along all four sides (the #202 lesson, same shape).
    pad = 2
    padded = np.pad(mask, pad, mode="edge")
    dist_px = ndimage.distance_transform_edt(padded)[pad:-pad, pad:-pad]
    dist_m = dist_px * float(pixel_size_m)

    labels, n = ndimage.label(mask)
    depth = np.zeros_like(result, dtype=np.float32)

    shelf_w = max(float(SEA_SHELF_WIDTH_M), 1e-6)
    for idx in range(1, n + 1):
        region = labels == idx
        if not region.any():
            continue
        reach_m = float(dist_m[region].max())
        # Ceiling scales with how far offshore this region reaches.
        t = min(reach_m / max(float(SEA_FULL_DEPTH_DISTANCE_M), 1e-6), 1.0)
        ceiling = SEA_MIN_DEPTH_M + t * (SEA_MAX_DEPTH_M - SEA_MIN_DEPTH_M)

        d = dist_m[region]
        shelf = np.minimum(d, shelf_w) / shelf_w * SEA_SHELF_DEPTH_M
        beyond = np.maximum(d - shelf_w, 0.0) * SEA_DROPOFF_SLOPE_M_PER_M
        depth[region] = np.minimum(shelf + beyond, ceiling)

    result[mask] = float(sea_surface_m) - depth[mask]
    return result


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
    water_mask_bool = water_mask.astype(bool, copy=False)
    if not water_mask_bool.any():
        return elevation

    labeled, n_features = ndimage.label(water_mask_bool)
    if n_features == 0:
        return elevation

    # Everything below works on the *water pixels only*, grouped by label.
    # The obvious `labeled == region_id` per region is an O(regions x N^2)
    # trap: a 4993x4993 Swedish tile with ~360 lakes scanned 9 billion cells
    # per loop and this function took 20 s (issue #185). Sorting the water
    # pixels by label once makes every per-region step touch only that
    # region's own pixels, so the total cost is O(water px).
    flat_idx = np.flatnonzero(labeled)
    labs = labeled.reshape(-1)[flat_idx]
    order = np.argsort(labs, kind="stable")
    flat_idx = flat_idx[order]
    labs = labs[order]
    del order, labeled          # ~130 MB of temporaries at 4993x4993

    region_ids = np.arange(1, n_features + 1)
    starts = np.searchsorted(labs, region_ids, side="left")
    ends = np.searchsorted(labs, region_ids, side="right")

    elev_sorted = elevation.reshape(-1)[flat_idx]

    # Water-surface level per region: 10th percentile of the region's source
    # elevations, robust against DEM/OSM misalignment leaving a peak inside
    # the polygon.
    levels = np.zeros(n_features + 1, dtype=np.float64)
    for i, (lo, hi) in enumerate(zip(starts, ends), start=1):
        if hi > lo:
            levels[i] = np.percentile(elev_sorted[lo:hi], 10)

    del elev_sorted

    # `.copy()` is C-ordered by definition, so `reshape(-1)` on it is a view
    # and these flat writes land in the array. Never reshape-and-assign into
    # an array you did not allocate here: on a non-contiguous one the reshape
    # silently returns a copy and the write goes nowhere.
    water_surface_field = elevation.copy()
    water_surface_field.reshape(-1)[flat_idx] = levels[labs]

    # Single global EDT, then scale per region by that region's maximum
    # shore distance so every region reaches its full `max_depth_m` at its
    # deepest interior point — small ponds get a shallow bowl, large lakes
    # a deep one, both with a continuous gradient.
    dist_px = parallel_edt(water_mask_bool)
    dist_sorted = dist_px.reshape(-1)[flat_idx]
    del dist_px                 # only the per-water-pixel values are needed

    max_dist = np.zeros(n_features + 1, dtype=np.float64)
    for i, (lo, hi) in enumerate(zip(starts, ends), start=1):
        if hi > lo:
            max_dist[i] = dist_sorted[lo:hi].max()

    region_depth_map = region_depth_map or {}
    ceilings = np.full(n_features + 1, float(max_depth_m), dtype=np.float64)
    for region_id, depth in region_depth_map.items():
        if 1 <= region_id <= n_features:
            ceilings[region_id] = float(depth)

    # Per-region cap: small ponds stay shallow (slope x radius caps depth
    # below max_depth_m), large lakes hit `ceiling` at their deepest point.
    # Either way the depth ramps linearly to that maximum.
    region_max_depth = np.minimum(
        ceilings, max_dist * pixel_size_m * shore_slope_m_per_m,
    )

    # A region with no shore distance at all is left at its source elevation,
    # exactly as the per-region loop did when it hit `max_d_px <= 0`.
    valid = max_dist > 0
    safe_max_dist = np.where(valid, max_dist, 1.0)
    norm = dist_sorted / safe_max_dist[labs]   # 0 at shore, 1 at deepest pt
    carved = levels[labs] - region_max_depth[labs] * norm

    result = elevation.copy()
    write = valid[labs]
    result.reshape(-1)[flat_idx[write]] = carved[write]

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
    # The 16-bit mode is derived from the array dtype, never passed as `mode=`:
    # Pillow removes that parameter in 13, and it reinterpreted the raw buffer
    # rather than converting, so a dtype drift would have silently emitted a
    # garbage heightmap. A C-contiguous uint16 array maps to an "I;16" image,
    # which Pillow writes as a 16-bit greyscale PNG - the format Enfusion's
    # terrain importer expects.
    heightmap = np.ascontiguousarray(heightmap, dtype=np.uint16)
    img = Image.fromarray(heightmap)
    img.save(output_path)
    logger.info(f"Saved heightmap PNG: {output_path} ({heightmap.shape[1]}x{heightmap.shape[0]})")
    return output_path


def save_heightmap_preview(heightmap: np.ndarray, output_path: str) -> str:
    """Save an 8-bit grayscale preview of the heightmap."""
    try:
        preview = (heightmap.astype(np.float32) / 256).astype(np.uint8)
        img = Image.fromarray(preview)  # uint8 -> "L", no `mode=` (Pillow 13)
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
        bbox_tuple = dem_bbox_wgs84(metadata)
        if bbox_tuple:
            road_mask = rasterize_features_to_mask(
                road_features,
                elevation.shape[1], elevation.shape[0],
                bbox_tuple,
                buffer_px=2,
            )
            elevation = flatten_roads_in_heightmap(
                elevation, road_mask, road_width_px=3, smooth_radius=5,
            )

    # Union of every water mask, kept for the land-datum shift in step 5b
    # (issue #165) so "lowest land" ignores carved lake and sea beds.
    # Issue #193: set when a qualifying open sea was carved, which changes
    # which elevation becomes world Y=0 (see compute_land_datum below).
    is_coastal_map = False
    # Kept as bool, not the rasterizers' uint8 — see compute_land_datum (#183).
    water_mask_union: np.ndarray | None = None

    # 4. Level water bodies — four passes, one per water type, so each
    # gets its own depth ceiling and rivers don't get merged into adjacent
    # lakes by the connected-component labelling.
    if water_features and water_features.get("features"):
        logger.info("Leveling water bodies...")
        if job:
            job.add_log(f"Leveling {len(water_features['features'])} water bodies...")
            job.progress = 53
        bbox_tuple = dem_bbox_wgs84(metadata)
        if bbox_tuple:
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
            # Issue #193: a sea gets a shelf-then-dropoff profile, not the
            # lake ramp, and only where it is genuinely open sea.
            sea_mask = qualifying_sea_regions(sea_mask)

            # Carve each type with its own depth ceiling. Order matters only
            # where masks overlap (a river crossing a lake gets overwritten
            # by the lake pass — desirable).
            # Shore slope, not max depth, is what decides how deep a small
            # water body actually gets: depth ramps linearly from the shore, so
            # a lake only reaches max_depth if it is max_depth/slope metres from
            # shore to centre. Lakes were carved at 0.3 m/m before #160, which
            # left typical inland lakes far shallower than their 8 m ceiling.
            if sea_mask.sum():
                sea_bool = sea_mask.astype(bool)
                water_mask_union = (
                    sea_bool if water_mask_union is None
                    else (water_mask_union | sea_bool)
                )
                is_coastal_map = True
                logger.info(
                    f"Carving sea bathymetry: {int(sea_mask.sum())} px "
                    f"({sea_mask.mean()*100:.1f}% of the map), "
                    f"{SEA_SHELF_DEPTH_M:.0f} m shelf over "
                    f"{SEA_SHELF_WIDTH_M:.0f} m then dropping to "
                    f"{SEA_MIN_DEPTH_M:.0f}-{SEA_MAX_DEPTH_M:.0f} m"
                )
                if job:
                    job.add_log(
                        f"Coastal map: carving sea bed under "
                        f"{sea_mask.mean()*100:.0f}% of the terrain",
                        "info",
                    )
                elevation = carve_sea_bathymetry(
                    elevation, sea_mask, target_resolution_m,
                )

            for mask, max_depth, slope, label in (
                (river_mask, 2.0, 0.3, "river/stream"),
                (wetland_mask, 1.0, 0.3, "wetland"),
                (
                    lake_mask,
                    LAKE_MAX_DEPTH_M,
                    LAKE_SHORE_SLOPE_M_PER_M,
                    "lake/pond/reservoir",
                ),
            ):
                if mask.sum() == 0:
                    continue
                mask_bool = mask.astype(bool)
                water_mask_union = (
                    mask_bool if water_mask_union is None
                    else (water_mask_union | mask_bool)
                )
                logger.info(
                    f"Carving bathymetry for {label} mask: "
                    f"{int(mask.sum())} px, max_depth={max_depth} m, "
                    f"shore_slope={slope} m/m"
                )
                elevation = flatten_water_in_heightmap(
                    elevation,
                    mask,
                    transition_px=3,
                    pixel_size_m=target_resolution_m,
                    max_depth_m=max_depth,
                    shore_slope_m_per_m=slope,
                )

    # 5. Light smoothing pass
    logger.info("Applying final smoothing...")
    if job:
        job.add_log("Applying final terrain smoothing...")
        job.progress = 55
    elevation = parallel_gaussian_filter(elevation, sigma=0.5)

    # 5b. Re-datum the terrain so the lowest LAND point becomes world Y = 0
    # (issue #165). For a coastal map the lowest land is already ~0 m, so this
    # is a no-op and sea level still lines up with the engine ocean plane. For
    # an inland map it is the difference between a terrain that sits on the
    # ground and one that floats hundreds of metres above it.
    absolute_min = float(np.min(elevation))
    absolute_max = float(np.max(elevation))
    if is_coastal_map:
        # Issue #193. On a coastal map mean sea level *is* world Y=0: the
        # engine's ocean plane is fixed there, so the water surface has to sit
        # on it with the floor below and the land above. Using the lowest land
        # instead (the #165 rule) shifts the whole terrain up by the height of
        # the lowest beach and pushes the carved sea floor with it, which is
        # what made the sea read as flat at Y=0.
        #
        # #165 still governs inland maps, where there is no ocean plane to
        # meet and the lowest land must become Y=0 or the terrain floats.
        land_datum = 0.0
        logger.info(
            "Coastal map: mean sea level (0 m) becomes world Y=0, so the "
            "carved sea bed is negative and meets the engine ocean plane"
        )
    else:
        land_datum = compute_land_datum(elevation, water_mask_union)
    if land_datum:
        elevation = elevation - land_datum
    logger.info(
        f"Land datum: lowest land {land_datum:.1f} m becomes world Y=0 "
        f"(absolute terrain span {absolute_min:.1f}–{absolute_max:.1f} m → "
        f"editor span {absolute_min - land_datum:.1f}–"
        f"{absolute_max - land_datum:.1f} m)"
    )
    if job:
        job.add_log(
            f"Zeroed terrain to lowest land: {land_datum:.1f} m above sea level "
            f"is now world Y=0",
            "info",
        )

    # 6. Normalise to 16-bit
    if job:
        job.progress = 56
    heightmap, height_info = generate_heightmap_from_array(elevation)
    height_info["land_datum_m"] = land_datum
    # Issue #193: records which datum rule applied, so the generated metadata
    # says whether this map was treated as coastal (sea level = Y=0) or inland
    # (lowest land = Y=0). Verifiable without re-running the pipeline.
    height_info["is_coastal_map"] = bool(is_coastal_map)
    height_info["absolute_min_elevation"] = absolute_min
    height_info["absolute_max_elevation"] = absolute_max

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
