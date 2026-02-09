"""
Surface mask generation service.

Generates per-material grayscale PNG masks for Enfusion surface painting.
Masks are derived from:
- OSM land use data (forests, farmland, urban, etc.)
- DEM analysis (slope -> rock, shoreline -> sand)
- Road buffers (asphalt/gravel strips)

Key improvements over original:
- Distance-field soft edges (scipy distance_transform_edt)
- Full normalization pass (all masks sum to 1.0 at every pixel)
- Block saturation analysis (max 5 surfaces per 33x33 block)
- Coverage statistics for metadata and SETUP_GUIDE
- Resolution-aware road buffer widths
- Resolution-scaled Gaussian sigma
- Recommended default surface and import order
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage

from config import TREELINE_ELEVATION
from config.enfusion import (
    BLOCK_VERTEX_SIZE,
    MAX_SURFACES_PER_BLOCK,
    BLOCK_SURFACE_THRESHOLD,
    SURFACE_MATERIAL_MAP,
    SURFACE_IMPORT_ORDER,
)
from services.heightmap_generator import rasterize_features_to_mask
from services.utils.parallel import parallel_gaussian_filter, parallel_edt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Soft edge utilities
# ---------------------------------------------------------------------------

def soft_edge_mask(binary_mask: np.ndarray, transition_px: int = 8) -> np.ndarray:
    """
    Convert a binary mask to a soft-edge float mask using distance transform.

    Creates smooth transitions at the boundaries of masked regions:
    - 1.0 deep inside the masked area
    - Gradual ramp from 1.0 to 0.0 over `transition_px` pixels at edges
    - 0.0 outside the masked area

    Args:
        binary_mask: Boolean or uint8 mask (non-zero = inside).
        transition_px: Width of the transition zone in pixels.

    Returns:
        Float array [0.0, 1.0] with soft edges.
    """
    if transition_px <= 0:
        return binary_mask.astype(np.float32)

    bool_mask = binary_mask.astype(bool)

    if not np.any(bool_mask):
        return np.zeros_like(binary_mask, dtype=np.float32)

    if np.all(bool_mask):
        return np.ones_like(binary_mask, dtype=np.float32)

    # Distance from nearest False pixel (grows inward from boundary)
    # Uses multi-threaded EDT via `edt` package when available
    inner_dist = parallel_edt(bool_mask)

    # Normalize: ramp from 0 at boundary to 1.0 at transition_px depth
    soft = np.clip(inner_dist / transition_px, 0.0, 1.0)
    return soft


def slope_ramp_mask(
    slope_deg: np.ndarray,
    start_deg: float = 25.0,
    full_deg: float = 40.0,
) -> np.ndarray:
    """
    Create a smooth ramp mask based on slope angle.

    Args:
        slope_deg: Slope angles in degrees.
        start_deg: Slope angle where coverage starts (0.0).
        full_deg: Slope angle where coverage reaches 1.0.

    Returns:
        Float array [0.0, 1.0].
    """
    if full_deg <= start_deg:
        return (slope_deg >= start_deg).astype(np.float32)

    return np.clip((slope_deg - start_deg) / (full_deg - start_deg), 0.0, 1.0)


# ---------------------------------------------------------------------------
# Block saturation analysis
# ---------------------------------------------------------------------------

def check_block_saturation(
    masks: dict[str, np.ndarray],
    block_size: int = BLOCK_VERTEX_SIZE,
    threshold: int = BLOCK_SURFACE_THRESHOLD,
) -> dict:
    """
    Check if any terrain block exceeds the max surface limit.

    Each block (33x33 vertices) supports at most 5 surfaces.
    A surface is counted if any pixel in the block exceeds the threshold.

    Args:
        masks: Dict mapping surface name to uint8 mask array.
        block_size: Block size in pixels (default 33 for Enfusion).
        threshold: Minimum pixel value to count as "meaningful coverage".

    Returns:
        Dict with violation count, total blocks, and per-block details.
    """
    if not masks:
        return {"violations": 0, "total_blocks": 0, "details": []}

    h, w = next(iter(masks.values())).shape
    violations = 0
    total_blocks = 0
    violation_details = []

    for y in range(0, h, block_size):
        for x in range(0, w, block_size):
            total_blocks += 1
            surfaces_in_block = 0
            surface_names = []

            for name, mask in masks.items():
                block = mask[y:y + block_size, x:x + block_size]
                if block.max() > threshold:
                    surfaces_in_block += 1
                    surface_names.append(name)

            if surfaces_in_block > MAX_SURFACES_PER_BLOCK:
                violations += 1
                violation_details.append({
                    "block_x": x // block_size,
                    "block_y": y // block_size,
                    "surfaces": surfaces_in_block,
                    "surface_names": surface_names,
                })

    return {
        "violations": violations,
        "total_blocks": total_blocks,
        "details": violation_details,
    }


def auto_merge_violations(
    masks: dict[str, np.ndarray],
    block_size: int = BLOCK_VERTEX_SIZE,
    threshold: int = BLOCK_SURFACE_THRESHOLD,
    default_surface: str = "grass",
) -> dict[str, np.ndarray]:
    """
    Auto-merge surfaces in blocks that exceed the 5-surface limit.

    In each violating block, the surface with the lowest maximum coverage
    is merged into the default surface.

    Args:
        masks: Dict mapping surface name to uint8 mask array.
        block_size: Block size in pixels.
        threshold: Minimum pixel value threshold.
        default_surface: Name of the default surface to merge into.

    Returns:
        Modified masks dict (modified in-place and returned).
    """
    saturation = check_block_saturation(masks, block_size, threshold)

    if saturation["violations"] == 0:
        return masks

    logger.info(f"Found {saturation['violations']} block saturation violations, auto-merging...")

    for detail in saturation["details"]:
        bx = detail["block_x"] * block_size
        by = detail["block_y"] * block_size

        # Find the surface with lowest max value in this block (excluding default)
        min_max_val = 256
        min_surface = None

        for name in detail["surface_names"]:
            if name == default_surface:
                continue
            block = masks[name][by:by + block_size, bx:bx + block_size]
            max_val = block.max()
            if max_val < min_max_val:
                min_max_val = max_val
                min_surface = name

        if min_surface:
            # Zero out the weakest surface in this block
            masks[min_surface][by:by + block_size, bx:bx + block_size] = 0
            logger.debug(
                f"Merged {min_surface} (max={min_max_val}) in block "
                f"({detail['block_x']}, {detail['block_y']})"
            )

    return masks


# ---------------------------------------------------------------------------
# Coverage statistics
# ---------------------------------------------------------------------------

def compute_coverage_stats(masks: dict[str, np.ndarray]) -> dict:
    """
    Compute coverage statistics for each surface mask.

    Args:
        masks: Dict mapping surface name to uint8 mask array.

    Returns:
        Dict with per-surface stats and recommended default.
    """
    if not masks:
        return {}

    h, w = next(iter(masks.values())).shape
    total_pixels = h * w

    coverage = {}
    for name, mask in masks.items():
        dominant_pixels = int((mask > 128).sum())
        any_pixels = int((mask > 0).sum())
        mean_val = float(mask.mean())

        coverage[name] = {
            "percentage": round(dominant_pixels / total_pixels * 100, 1),
            "any_coverage_pct": round(any_pixels / total_pixels * 100, 1),
            "pixels_dominant": dominant_pixels,
            "pixels_any": any_pixels,
            "mean_value": round(mean_val, 1),
        }

    # Determine recommended default surface
    # The default should be the surface with the highest coverage
    recommended_default = max(coverage, key=lambda k: coverage[k]["percentage"])

    # Override: for mountain terrains, use rock if it's significant
    rock_pct = coverage.get("rock", {}).get("percentage", 0)
    grass_pct = coverage.get("grass", {}).get("percentage", 0)

    if rock_pct > 40:
        recommended_default = "rock"
    elif grass_pct > 50:
        recommended_default = "grass"

    return {
        "per_surface": coverage,
        "recommended_default": recommended_default,
        "recommended_default_material": SURFACE_MATERIAL_MAP.get(
            recommended_default, "Grass_01.emat"
        ),
        "import_order": SURFACE_IMPORT_ORDER,
    }


# ---------------------------------------------------------------------------
# Helper: generate slope mask
# ---------------------------------------------------------------------------

def generate_slope_mask(elevation: np.ndarray, cell_size_m: float = 2.0) -> np.ndarray:
    """
    Generate slope angle mask from DEM.

    Returns array of slope angles in degrees.
    """
    dy, dx = np.gradient(elevation, cell_size_m)
    slope_rad = np.arctan(np.sqrt(dx**2 + dy**2))
    slope_deg = np.degrees(slope_rad)
    return slope_deg


# ---------------------------------------------------------------------------
# Helper: rasterization wrappers
# ---------------------------------------------------------------------------

def _rasterize_polygons(
    features: dict | None,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    filter_tags: dict[str, list[str]] | None = None,
) -> np.ndarray:
    """
    Fast polygon rasterization wrapper.
    Returns a boolean mask (True where polygons exist).
    """
    if not features or not features.get("features"):
        return np.zeros((height, width), dtype=bool)

    mask = rasterize_features_to_mask(
        features, width, height, bounds,
        filter_tags=filter_tags,
    )
    return mask.astype(bool)


def _rasterize_lines(
    features: dict | None,
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    buffer_px: int = 2,
    filter_tags: dict[str, list[str]] | None = None,
) -> np.ndarray:
    """
    Fast line rasterization wrapper.
    Returns a boolean mask (True where lines exist, including buffer).
    """
    if not features or not features.get("features"):
        return np.zeros((height, width), dtype=bool)

    mask = rasterize_features_to_mask(
        features, width, height, bounds,
        filter_tags=filter_tags,
        buffer_px=buffer_px,
    )
    return mask.astype(bool)


# ---------------------------------------------------------------------------
# Main generation function
# ---------------------------------------------------------------------------

def generate_surface_masks(
    elevation: np.ndarray,
    osm_data: dict,
    bounds: tuple[float, float, float, float],
    cell_size_m: float = 2.0,
    output_dir: Optional[Path] = None,
    country_code: str = "UNKNOWN",
    heightmap_dimensions: Optional[tuple[int, int]] = None,
    job=None,
) -> dict:
    """
    Generate all surface masks for Enfusion.

    Produces up to 5 normalized surface masks with soft edge transitions:
    1. Grass (default complement — fills wherever no other surface claims)
    2. Forest floor (under forest areas)
    3. Asphalt/road surface (paved roads + urban)
    4. Rock (steep slopes + above treeline)
    5. Sand/dirt (near water, farmland, gravel roads)

    The masks are mutually normalized: at every pixel, all mask values
    sum to exactly 255, ensuring clean Enfusion import regardless of order.

    Args:
        elevation: DEM numpy array.
        osm_data: Dict with roads, water, forests, buildings, land_use.
        bounds: (west, south, east, north) in WGS84 degrees.
        cell_size_m: Grid cell size in metres.
        output_dir: Output directory for mask PNGs.
        country_code: ISO country code for country-specific rules.
        heightmap_dimensions: (width, height) to ensure masks match heightmap.
        job: Optional MapGenerationJob for logging.

    Returns:
        Dict with mask file paths, coverage stats, block saturation info.
    """
    if output_dir is None:
        import tempfile
        output_dir = Path(tempfile.mkdtemp())

    h, w = elevation.shape

    # If heightmap_dimensions specified, verify or resize
    if heightmap_dimensions is not None:
        target_w, target_h = heightmap_dimensions
        if (w, h) != (target_w, target_h):
            logger.info(
                f"Resizing elevation for mask generation: {w}x{h} -> {target_w}x{target_h}"
            )
            from services.utils.parallel import parallel_zoom
            zoom_y = target_h / h
            zoom_x = target_w / w
            elevation = parallel_zoom(elevation, (zoom_y, zoom_x), order=3)
            h, w = elevation.shape

    logger.info(f"Generating surface masks for {w}x{h} terrain (cell size: {cell_size_m}m)")
    if job:
        job.add_log(f"Generating surface masks for {w}x{h} terrain (cell size: {cell_size_m}m)...")

    # Resolution-scaled parameters
    sigma = max(1.0, 2.0 * (cell_size_m / 2.0))
    road_buffer_px = max(2, int(6.0 / cell_size_m))    # ~6m paved road half-width
    gravel_buffer_px = max(1, int(3.0 / cell_size_m))   # ~3m gravel road half-width
    forest_transition_px = max(3, int(20.0 / cell_size_m))  # ~20m forest edge transition
    sand_transition_px = max(2, int(10.0 / cell_size_m))    # ~10m shoreline transition

    logger.debug(
        f"Resolution-scaled params: sigma={sigma:.1f}, road_buf={road_buffer_px}px, "
        f"gravel_buf={gravel_buffer_px}px, forest_edge={forest_transition_px}px"
    )

    # =========================================================================
    # Step 1: Compute DEM-derived features
    # =========================================================================
    if job:
        job.add_log("Computing slope and aspect from elevation...")
    logger.debug("Computing slope mask from DEM...")
    slope = generate_slope_mask(elevation, cell_size_m)

    # Get treeline for this country (latitude-interpolated if possible)
    treeline = _get_treeline_elevation(country_code, bounds)
    logger.debug(f"Using treeline elevation: {treeline}m for country {country_code}")
    if job:
        job.add_log(f"Using treeline elevation: {treeline}m for {country_code}")

    # =========================================================================
    # Step 2: Generate raw binary masks from data sources
    # =========================================================================
    # Count features for logging
    n_forest = len((osm_data.get("forests") or {}).get("features", []))
    n_roads = len((osm_data.get("roads") or {}).get("features", []))
    n_water = len((osm_data.get("water") or {}).get("features", []))
    n_buildings = len((osm_data.get("buildings") or {}).get("features", []))
    n_land_use = len((osm_data.get("land_use") or {}).get("features", []))

    if job:
        job.add_log(
            f"Rasterizing geographic features ({n_roads} roads, {n_water} water, "
            f"{n_forest} forests, {n_buildings} buildings, {n_land_use} land use)..."
        )
        job.progress = 62

    # Forest polygons
    logger.debug("Rasterizing forest areas...")
    forest_binary = _rasterize_polygons(osm_data.get("forests"), bounds, w, h)

    # Paved roads
    logger.debug("Rasterizing road network...")
    if job:
        job.progress = 64
    asphalt_types = [
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "residential", "unclassified", "service", "motorway_link",
        "trunk_link", "primary_link", "secondary_link", "tertiary_link",
        "living_street", "cycleway",
    ]
    asphalt_binary = _rasterize_lines(
        osm_data.get("roads"), bounds, w, h,
        buffer_px=road_buffer_px,
        filter_tags={"highway": asphalt_types},
    )

    # Gravel/dirt roads
    gravel_types = ["track", "path", "footway", "bridleway"]
    gravel_binary = _rasterize_lines(
        osm_data.get("roads"), bounds, w, h,
        buffer_px=gravel_buffer_px,
        filter_tags={"highway": gravel_types},
    )

    # Water bodies (for sand/shoreline transition)
    if job:
        job.progress = 66
    water_binary = _rasterize_polygons(
        osm_data.get("water"), bounds, w, h,
        filter_tags={"water_type": ["lake", "pond", "reservoir", "water"]},
    )

    # Farmland areas
    farmland_binary = _rasterize_polygons(
        osm_data.get("land_use"), bounds, w, h,
        filter_tags={"type": ["farmland", "farmyard", "allotments", "orchard"]},
    )

    # Urban areas + buildings
    urban_binary = _rasterize_polygons(
        osm_data.get("land_use"), bounds, w, h,
        filter_tags={"type": ["residential", "industrial", "commercial", "retail"]},
    )
    building_binary = _rasterize_polygons(osm_data.get("buildings"), bounds, w, h)
    urban_combined = urban_binary | building_binary

    # =========================================================================
    # Step 3: Compute soft-edge float masks [0.0, 1.0]
    # =========================================================================
    if job:
        job.add_log("Computing soft-edge surface transitions (parallel)...")
        job.progress = 68

    logger.debug("Computing soft-edge masks (parallel)...")

    # Precompute treeline mask (needed by rock and forest)
    treeline_mask = np.clip((elevation - treeline) / 200.0, 0.0, 1.0)

    # --- Define per-surface compute functions for parallel execution ---
    # scipy.ndimage operations (distance_transform_edt, gaussian_filter)
    # release the GIL, so threads achieve true parallelism for these.

    def _compute_rock():
        rock = slope_ramp_mask(slope, start_deg=25.0, full_deg=40.0)
        rock = np.maximum(rock, treeline_mask)
        return ndimage.gaussian_filter(rock, sigma=sigma)

    def _compute_forest():
        forest = soft_edge_mask(forest_binary, transition_px=forest_transition_px)
        forest *= (1.0 - treeline_mask)
        return forest

    def _compute_asphalt():
        asphalt_soft = soft_edge_mask(asphalt_binary, transition_px=2)
        urban_soft = soft_edge_mask(urban_combined, transition_px=3) * 0.5
        return np.maximum(asphalt_soft, urban_soft)

    def _compute_sand_dirt():
        if np.any(water_binary):
            water_dilated = ndimage.binary_dilation(water_binary, iterations=sand_transition_px)
            shore_zone = water_dilated & ~water_binary
            sand_shore = soft_edge_mask(shore_zone, transition_px=sand_transition_px)
        else:
            sand_shore = np.zeros((h, w), dtype=np.float32)
        farmland_soft = soft_edge_mask(farmland_binary, transition_px=5) * 0.6
        gravel_soft = soft_edge_mask(gravel_binary, transition_px=2) * 0.8
        return np.maximum(sand_shore, np.maximum(farmland_soft, gravel_soft))

    # Run all 4 mask computations in parallel threads
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_rock = pool.submit(_compute_rock)
        future_forest = pool.submit(_compute_forest)
        future_asphalt = pool.submit(_compute_asphalt)
        future_sand = pool.submit(_compute_sand_dirt)

        rock_float = future_rock.result()
        forest_float = future_forest.result()
        asphalt_float = future_asphalt.result()
        sand_dirt_float = future_sand.result()

    # =========================================================================
    # Step 4: Normalize all masks to sum to 1.0 at every pixel
    # =========================================================================
    if job:
        job.add_log("Normalizing surface masks (ensuring clean Enfusion import)...")
        job.progress = 70

    logger.debug("Normalizing surface masks...")

    # Water pixels get zeroed out (water is handled by WaterEntity, not surfaces)
    water_exclusion = ~water_binary

    # Apply water exclusion
    rock_float *= water_exclusion
    forest_float *= water_exclusion
    asphalt_float *= water_exclusion
    sand_dirt_float *= water_exclusion

    # Stack non-grass masks
    mask_stack = np.stack([rock_float, forest_float, asphalt_float, sand_dirt_float], axis=-1)

    # Total of all non-grass masks at each pixel
    total_non_grass = mask_stack.sum(axis=-1)

    # Where total > 1.0, normalize proportionally
    oversaturated = total_non_grass > 1.0
    if np.any(oversaturated):
        # Use np.maximum to avoid divide-by-zero for pixels where
        # total_non_grass is 0 (they aren't oversaturated, so the
        # scale value is discarded, but np.where evaluates both
        # branches and the division would trigger a RuntimeWarning).
        safe_total = np.maximum(total_non_grass, 1e-10)
        scale = np.where(oversaturated, 1.0 / safe_total, 1.0)
        mask_stack *= scale[..., np.newaxis]
        total_non_grass = mask_stack.sum(axis=-1)

    # Grass is the complement: whatever's left after all other surfaces
    grass_float = np.clip(1.0 - total_non_grass, 0.0, 1.0)
    grass_float *= water_exclusion  # No grass on water either

    # Unpack normalized masks
    rock_float = mask_stack[..., 0]
    forest_float = mask_stack[..., 1]
    asphalt_float = mask_stack[..., 2]
    sand_dirt_float = mask_stack[..., 3]

    # =========================================================================
    # Step 5: Convert to uint8 and save
    # =========================================================================
    if job:
        job.add_log("Saving surface mask files...")
        job.progress = 72

    def to_uint8(f: np.ndarray) -> np.ndarray:
        return np.clip(f * 255, 0, 255).astype(np.uint8)

    mask_arrays = {
        "grass": to_uint8(grass_float),
        "forest_floor": to_uint8(forest_float),
        "asphalt": to_uint8(asphalt_float),
        "rock": to_uint8(rock_float),
        "sand_dirt": to_uint8(sand_dirt_float),
    }

    # =========================================================================
    # Step 6: Block saturation check and auto-merge
    # =========================================================================
    if job:
        job.progress = 73

    saturation_before = check_block_saturation(mask_arrays)
    if saturation_before["violations"] > 0:
        logger.info(
            f"Block saturation: {saturation_before['violations']} violations "
            f"out of {saturation_before['total_blocks']} blocks, auto-merging..."
        )
        if job:
            job.add_log(
                f"Found {saturation_before['violations']} block saturation violations, "
                f"auto-merging to stay within 5-surface limit..."
            )

        # Determine default surface for merging
        coverage_quick = {
            name: (mask > 128).sum()
            for name, mask in mask_arrays.items()
        }
        default_surface = max(coverage_quick, key=coverage_quick.get)

        mask_arrays = auto_merge_violations(
            mask_arrays,
            default_surface=default_surface,
        )

        saturation_after = check_block_saturation(mask_arrays)
        logger.info(
            f"After merge: {saturation_after['violations']} violations remaining"
        )

    saturation_final = check_block_saturation(mask_arrays)

    # =========================================================================
    # Step 7: Save mask files
    # =========================================================================
    masks = {}

    def _save_mask(name: str, data: np.ndarray):
        path = output_dir / f"surface_{name}.png"
        img = Image.fromarray(data, mode="L")
        img.save(str(path))
        masks[name] = str(path)

    for name, array in mask_arrays.items():
        _save_mask(name, array)
    if job:
        job.add_log(f"Saved {len(mask_arrays)} surface mask files")

    # =========================================================================
    # Step 8: Compute coverage statistics
    # =========================================================================
    coverage_stats = compute_coverage_stats(mask_arrays)

    logger.info(
        f"Surface coverage: "
        + ", ".join(
            f"{name}: {stats['percentage']}%"
            for name, stats in coverage_stats["per_surface"].items()
        )
    )
    logger.info(
        f"Recommended default: {coverage_stats['recommended_default']} "
        f"({coverage_stats['recommended_default_material']})"
    )
    logger.info(
        f"Block saturation: {saturation_final['violations']} violations "
        f"out of {saturation_final['total_blocks']} blocks"
    )

    # =========================================================================
    # Step 9: Generate combined preview
    # =========================================================================
    try:
        preview = np.zeros((h, w, 3), dtype=np.uint8)
        # Green channel = grass
        preview[:, :, 1] = mask_arrays["grass"]
        # Red channel = forest + rock blend
        preview[:, :, 0] = np.clip(
            mask_arrays["forest_floor"].astype(np.int16) * 0.6
            + mask_arrays["rock"].astype(np.int16) * 0.4,
            0, 255,
        ).astype(np.uint8)
        # Blue channel = asphalt + sand blend
        preview[:, :, 2] = np.clip(
            mask_arrays["asphalt"].astype(np.int16)
            + mask_arrays["sand_dirt"].astype(np.int16) * 0.5,
            0, 255,
        ).astype(np.uint8)
        # Water overlay
        preview[water_binary] = [30, 30, 200]

        preview_path = output_dir / "surface_preview.png"
        Image.fromarray(preview, mode="RGB").save(str(preview_path))
        masks["preview"] = str(preview_path)
        logger.info(f"Saved surface preview: {preview_path}")
    except Exception as e:
        logger.error(f"Failed to generate surface preview: {e}", exc_info=True)
        masks["preview"] = None

    # =========================================================================
    # Log summary
    # =========================================================================
    if job:
        default_name = coverage_stats["recommended_default"]
        default_pct = coverage_stats["per_surface"].get(default_name, {}).get("percentage", 0)
        job.add_log(
            f"Surface masks generated: "
            + ", ".join(
                f"{name} {stats['percentage']}%"
                for name, stats in coverage_stats["per_surface"].items()
            ),
            "success"
        )
        job.add_log(
            f"Recommended default surface: {default_name} ({default_pct}%). "
            f"Block violations: {saturation_final['violations']}/{saturation_final['total_blocks']}",
            "success"
        )

    return {
        "masks": masks,
        "mask_count": len(masks) - (1 if "preview" in masks else 0),
        "dimensions": f"{w}x{h}",
        "surfaces": list(mask_arrays.keys()),
        "coverage": coverage_stats,
        "block_saturation": {
            "violations": saturation_final["violations"],
            "total_blocks": saturation_final["total_blocks"],
        },
    }


# ---------------------------------------------------------------------------
# Helper: latitude-interpolated treeline
# ---------------------------------------------------------------------------

def _get_treeline_elevation(country_code: str, bounds: tuple) -> int:
    """
    Get treeline elevation, interpolated by latitude if possible.

    For countries with latitude-dependent treelines (like Norway, where
    treeline ranges from ~1200m in the south to ~900m in the north),
    interpolate based on the centre latitude of the bounding box.

    Args:
        country_code: ISO country code.
        bounds: (west, south, east, north) bounding box.

    Returns:
        Treeline elevation in metres.
    """
    base_treeline = TREELINE_ELEVATION.get(country_code, 1200)

    # Country-specific latitude interpolation
    TREELINE_RANGES = {
        # country: (min_lat, max_lat, treeline_at_min_lat, treeline_at_max_lat)
        "NO": (58.0, 71.0, 1200, 800),    # Southern Norway -> Northern Norway
        "SE": (55.5, 69.0, 1100, 800),     # Southern Sweden -> Northern Sweden
        "FI": (60.0, 70.0, 700, 400),      # Southern Finland -> Northern Finland
    }

    if country_code in TREELINE_RANGES:
        min_lat, max_lat, treeline_south, treeline_north = TREELINE_RANGES[country_code]
        center_lat = (bounds[1] + bounds[3]) / 2  # (south + north) / 2

        # Clamp latitude to range
        t = np.clip((center_lat - min_lat) / (max_lat - min_lat), 0.0, 1.0)
        # Interpolate: south has higher treeline, north has lower
        treeline = int(treeline_south + t * (treeline_north - treeline_south))

        logger.debug(
            f"Treeline interpolation for {country_code}: "
            f"lat={center_lat:.1f} -> treeline={treeline}m "
            f"(range: {treeline_south}m @ {min_lat}N to {treeline_north}m @ {max_lat}N)"
        )
        return treeline

    return base_treeline
