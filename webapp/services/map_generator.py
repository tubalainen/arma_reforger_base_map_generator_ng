"""
Map generation orchestrator.

Coordinates the full pipeline from polygon input to Arma Reforger export:
1. Country detection
2. Elevation data acquisition
3. OSM feature extraction
4. Heightmap generation (with road flattening + water leveling)
5. Surface mask generation
6. Satellite imagery download
7. Road processing
8. Feature extraction
9. Export packaging

Each step is implemented as an independent function for testability.
The orchestrator (run_generation) coordinates them and tracks progress.
"""

import asyncio
import json
import logging
import secrets
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Job ID length in bytes (16 bytes = 128 bits of entropy, URL-safe base64 encoded)
JOB_ID_BYTES = 16


# ---------------------------------------------------------------------------
# Job model and storage
# ---------------------------------------------------------------------------

class MapGenerationJob:
    """Represents a map generation job with progress tracking."""

    def __init__(
        self, job_id: str, polygon_coords: list, options: dict, session_id: str
    ):
        self.job_id = job_id
        self.polygon_coords = polygon_coords
        self.options = options
        self.session_id = session_id  # Owner session for access control
        self.status = "pending"
        self.progress = 0
        self.current_step = ""
        self.steps_completed = []
        self.logs = []  # Activity log messages for frontend display
        self.errors = []
        self.result = None
        self.created_at = datetime.utcnow().isoformat()
        self.completed_at = None

    def add_log(self, message: str, level: str = "info"):
        """
        Add a log message to the activity log.

        Args:
            message: The log message
            level: Log level (info, success, warning, error)
        """
        self.logs.append({
            "timestamp": datetime.utcnow().isoformat(),
            "level": level,
            "message": message,
        })

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "current_step": self.current_step,
            "steps_completed": self.steps_completed,
            "logs": self.logs,
            "errors": self.errors,
            "result": self.result,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


# Global job storage (in production, use Redis or a database)
_jobs: dict[str, MapGenerationJob] = {}
_jobs_lock = threading.RLock()

# Track active downloads to prevent cleanup during file access
_active_downloads: set[str] = set()
_downloads_lock = threading.RLock()


def get_job(job_id: str) -> Optional[MapGenerationJob]:
    """Get a job by ID (thread-safe)."""
    with _jobs_lock:
        return _jobs.get(job_id)


def create_job(
    polygon_coords: list, options: dict, session_id: str
) -> MapGenerationJob:
    """
    Create a new map generation job.

    Args:
        polygon_coords: List of [lng, lat] coordinates defining the area
        options: Generation options (heightmap_size, grid_resolution, etc.)
        session_id: Session ID of the user creating the job

    Returns:
        The created MapGenerationJob
    """
    # Generate a cryptographically secure job ID
    job_id = secrets.token_urlsafe(JOB_ID_BYTES)
    job = MapGenerationJob(job_id, polygon_coords, options, session_id)

    with _jobs_lock:
        _jobs[job_id] = job

    logger.info(f"Created job {job_id[:8]}... for session {session_id[:8]}...")
    return job


def cleanup_job_session(job_id: str) -> None:
    """
    Remove the session association from a job when the session expires.

    This prevents issues where a job references a non-existent session.
    The job itself remains accessible via its ID for the cleanup period.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            logger.debug(f"Cleared session association for job {job_id[:8]}...")
            # Keep the job but clear the session reference
            job.session_id = None


def mark_download_active(job_id: str) -> None:
    """Mark a job as having an active download."""
    with _downloads_lock:
        _active_downloads.add(job_id)
        logger.debug(f"Marked job {job_id[:8]}... as downloading")


def mark_download_complete(job_id: str) -> None:
    """Mark a job download as complete."""
    with _downloads_lock:
        _active_downloads.discard(job_id)
        logger.debug(f"Marked job {job_id[:8]}... download complete")


def is_download_active(job_id: str) -> bool:
    """Check if a job has an active download."""
    with _downloads_lock:
        return job_id in _active_downloads


# ---------------------------------------------------------------------------
# Pipeline step functions (pure logic, no job state)
# ---------------------------------------------------------------------------

async def step_detect_countries(polygon_coords: list) -> dict:
    """Step 1: Detect which countries the polygon intersects."""
    from services.country_detector import detect_countries
    return await detect_countries(polygon_coords)


async def step_fetch_elevation(
    bbox: dict,
    primary_country: str,
    job: Optional[MapGenerationJob] = None,
    max_pixels: int | None = None,
) -> dict:
    """Step 2: Download elevation data from the best available source."""
    from services.elevation_service import fetch_elevation

    result = await fetch_elevation(bbox, primary_country, job, max_pixels=max_pixels)
    if not result["data"]:
        raise RuntimeError("Failed to fetch elevation data from any source")
    return result


async def step_fetch_osm(
    bbox: dict,
    output_dir: Path,
    job: Optional[MapGenerationJob] = None,
) -> dict:
    """Step 3: Fetch OSM features and save raw GeoJSON."""
    from services.osm_service import fetch_all_features

    osm_data = await fetch_all_features(bbox, job)

    for name, data in osm_data.items():
        with open(output_dir / f"osm_{name}.geojson", "w") as f:
            json.dump(data, f)

    return osm_data


def step_generate_heightmap(
    dem_bytes: bytes,
    osm_data: dict,
    target_size: int | tuple[int, int],
    target_resolution: float,
    output_dir: Path,
    job: Optional[MapGenerationJob] = None,
) -> dict:
    """Step 4: Generate heightmap with road flattening and water leveling.

    Args:
        target_size: Output heightmap dimensions.
            - int: square heightmap (size x size)
            - tuple (size_x, size_z): non-square heightmap (width x height)

    Returns result dict including '_elevation_array' and '_dem_metadata'
    for reuse in step 5 (avoids re-parsing the DEM).
    """
    from services.heightmap_generator import generate_heightmap

    return generate_heightmap(
        dem_bytes=dem_bytes,
        road_features=osm_data.get("roads"),
        water_features=osm_data.get("water"),
        target_size=target_size,
        target_resolution_m=target_resolution,
        output_dir=output_dir,
        job=job,
    )


def step_generate_surface_masks(
    elevation_array,
    osm_data: dict,
    bbox: dict,
    target_resolution: float,
    output_dir: Path,
    primary_country: str,
    heightmap_dimensions: Optional[tuple[int, int]] = None,
    job: Optional[MapGenerationJob] = None,
) -> dict:
    """Step 5: Generate surface masks using the elevation array from step 4."""
    from services.surface_mask_generator import generate_surface_masks

    return generate_surface_masks(
        elevation=elevation_array,
        osm_data=osm_data,
        bounds=(bbox["west"], bbox["south"], bbox["east"], bbox["north"]),
        cell_size_m=target_resolution,
        output_dir=output_dir,
        country_code=primary_country,
        heightmap_dimensions=heightmap_dimensions,
        job=job,
    )


def step_process_roads(
    osm_data: dict,
    primary_country: str,
    output_dir: Path,
    transformer=None,
    elevation_array=None,
    job: Optional[MapGenerationJob] = None,
) -> dict:
    """Step 6: Classify roads and export Enfusion-ready data."""
    from services.road_processor import (
        process_roads, export_roads_geojson, export_roads_spline_csv,
        export_roads_geojson_local, export_roads_reference_csv,
    )

    road_result = process_roads(
        road_features=osm_data.get("roads", {}),
        country_code=primary_country,
        job=job,
    )

    # Export original WGS84 GeoJSON
    road_geojson = export_roads_geojson(road_result)
    with open(output_dir / "roads_enfusion.geojson", "w") as f:
        json.dump(road_geojson, f)

    # Export with local coordinates if transformer is available
    if transformer:
        road_geojson_local = export_roads_geojson_local(road_result, transformer)
        with open(output_dir / "roads_enfusion_local.geojson", "w") as f:
            json.dump(road_geojson_local, f)

    # Export CSV (with local coords if transformer available)
    road_csv = export_roads_spline_csv(road_result, transformer=transformer)
    with open(output_dir / "roads_splines.csv", "w") as f:
        f.write(road_csv)

    # Export roads reference CSV for manual prefab setup in Workbench
    reference_csv = export_roads_reference_csv(road_result)
    with open(output_dir / "roads_reference.csv", "w") as f:
        f.write(reference_csv)

    return road_result


async def step_fetch_satellite_imagery(
    bbox: dict,
    target_size_x: int,
    target_size_z: int,
    output_dir: Path,
    country_codes: list[str] | None = None,
    job: Optional[MapGenerationJob] = None,
) -> dict:
    """Step 7: Fetch satellite imagery for the area.

    Uses country-aware dispatch: Swedish maps try Lantmäteriet orthophotos
    (sub-meter) first, falling back to Sentinel-2 Cloudless (10 m).

    Args:
        target_size_x: Satellite image width in pixels (matches heightmap X).
        target_size_z: Satellite image height in pixels (matches heightmap Z).
    """
    from services.satellite_service import fetch_satellite_imagery

    width = target_size_x
    height = target_size_z

    bbox_tuple = (bbox["west"], bbox["south"], bbox["east"], bbox["north"])

    satellite_data, source_name = await fetch_satellite_imagery(
        bbox_tuple, width, height, country_codes=country_codes, job=job
    )

    if satellite_data:
        satellite_path = output_dir / "satellite_map.png"

        # Validate and convert to proper PNG — WMS may return JPEG
        # despite FORMAT=image/png request.
        # Also ensure correct dimensions and DPI metadata for Enfusion import.
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(satellite_data))
            original_format = img.format  # e.g. "JPEG", "PNG"
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Resize to match heightmap dimensions if different
            if img.size != (width, height):
                logger.info(
                    f"Resizing satellite image from {img.size[0]}x{img.size[1]} "
                    f"to {width}x{height} to match heightmap"
                )
                img = img.resize((width, height), Image.LANCZOS)

            # Save with DPI metadata (required by Enfusion Workbench for import)
            img.save(str(satellite_path), format="PNG", dpi=(96, 96))
            actual_dims = f"{img.size[0]}x{img.size[1]}"
            logger.info(
                f"Saved satellite image as PNG ({actual_dims}, dpi=96, "
                f"original format: {original_format})"
            )
        except Exception as e:
            logger.warning(f"Failed to validate/convert satellite image: {e}, saving raw bytes")
            with open(satellite_path, "wb") as f:
                f.write(satellite_data)
            actual_dims = f"{width}x{height}"

        return {
            "success": True,
            "file": "satellite_map.png",
            "size_bytes": len(satellite_data),
            "dimensions": actual_dims,
            "source": source_name,
        }
    else:
        logger.warning("Failed to fetch satellite imagery, continuing without it")
        return {
            "success": False,
            "file": None,
            "dimensions": f"{width}x{height}",
            "source": None,
        }


def step_extract_features(osm_data: dict, primary_country: str, output_dir: Path, job: Optional[MapGenerationJob] = None) -> dict:
    """Step 8: Extract structured features (water, forests, buildings)."""
    from services.feature_extractor import extract_all_features

    features = extract_all_features(osm_data, primary_country, job)

    with open(output_dir / "features.json", "w") as f:
        json.dump(features, f, default=str)

    return features


def build_metadata(
    polygon_coords: list,
    options: dict,
    country_info: dict,
    elevation_result: dict,
    heightmap_result: dict,
    surface_result: dict,
    road_result: dict,
    features: dict,
    satellite_result: dict = None,
    coordinate_transform: dict = None,
    map_name: str = None,
) -> dict:
    """Assemble the output metadata.json content."""
    metadata = {
        "generator": "Arma Reforger Base Map Generator",
        "version": "1.0.0",
        "generated_at": datetime.utcnow().isoformat(),
        "input": {
            "polygon_coords": polygon_coords,
            "bbox": country_info["bbox"],
            "countries": country_info["countries"],
            "primary_country": country_info["primary_country"],
            "crs": country_info["crs"],
        },
        "options": options,
        "elevation": {
            "source": elevation_result["source"],
            "resolution_m": elevation_result["resolution_m"],
            "min_elevation_m": heightmap_result["min_elevation"],
            "max_elevation_m": heightmap_result["max_elevation"],
            "height_scale": heightmap_result["height_scale"],
            "height_offset": heightmap_result["height_offset"],
        },
        "heightmap": {
            "dimensions": heightmap_result["dimensions"],
            "terrain_size_m": heightmap_result["terrain_size_m"],
            "grid_cell_size_m": heightmap_result["grid_cell_size_m"],
            "format": "16-bit PNG + ESRI ASCII Grid",
        },
        "surface_masks": {
            "count": surface_result["mask_count"],
            "surfaces": surface_result["surfaces"],
            "format": "8-bit grayscale PNG",
            "coverage": surface_result.get("coverage", {}),
            "block_saturation": surface_result.get("block_saturation", {}),
        },
        "roads": {
            "total_segments": road_result.get("stats", {}).get("total", 0),
            "by_surface": road_result.get("stats", {}).get("by_surface", {}),
            "by_type": road_result.get("stats", {}).get("by_type", {}),
        },
        "features": features["summary"],
        "enfusion_import": {
            "heightmap_file": "heightmap.asc",
            "heightmap_png": "heightmap.png",
            "surface_masks": [f"surface_{s}.png" for s in surface_result["surfaces"]],
            "road_data": "roads_enfusion.geojson",
            "road_splines": "roads_splines.csv",
            "recommended_settings": {
                "terrain_size": heightmap_result["terrain_size_m"],
                "grid_cell_size": heightmap_result["grid_cell_size_m"],
                "height_scale": heightmap_result["height_scale"],
                "height_offset": heightmap_result["height_offset"],
                "invert_x_axis": False,
                "invert_z_axis": True,
            },
        },
    }

    # Add satellite imagery info if available
    if satellite_result and satellite_result.get("success"):
        metadata["satellite"] = {
            "file": satellite_result["file"],
            "source": satellite_result.get("source", "Sentinel-2 Cloudless (EOX)"),
            "dimensions": satellite_result["dimensions"],
            "format": "PNG",
        }
        metadata["enfusion_import"]["satellite_map"] = satellite_result["file"]

    # Add coordinate transformation info
    if coordinate_transform:
        metadata["coordinate_transform"] = coordinate_transform

    # Add map name
    if map_name:
        metadata["map_name"] = map_name

    return metadata


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_generation(job: MapGenerationJob):
    """
    Execute the full map generation pipeline.

    This runs as a background task and updates the job status as it progresses.
    Each step is delegated to a standalone function; the orchestrator only
    manages progress tracking and data flow between steps.
    """
    from config import OUTPUT_DIR

    output_dir = OUTPUT_DIR / job.job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        job.status = "running"

        # Step 1: Detect countries (0% -> 10%)
        job.current_step = "Detecting countries..."
        job.progress = 0
        logger.info(f"[{job.job_id}] Step 1: Country detection")
        job.add_log("Detecting countries in selected area...")

        country_info = await step_detect_countries(job.polygon_coords)
        primary_country = country_info["primary_country"]
        bbox = country_info["bbox"]

        job.progress = 10
        job.steps_completed.append({
            "step": "country_detection",
            "countries": country_info["countries"],
            "primary_country": primary_country,
            "crs": country_info["crs"],
        })
        logger.info(
            f"[{job.job_id}] Countries: {country_info['countries']}, "
            f"Primary: {primary_country}, CRS: {country_info['crs']}"
        )
        job.add_log(
            f"Detected countries: {', '.join(country_info['countries'])} (CRS: {country_info['crs']})",
            "success"
        )

        # Step 2: Fetch elevation data (10% -> 25%)
        job.current_step = f"Downloading elevation data ({primary_country})..."
        job.progress = 10
        logger.info(f"[{job.job_id}] Step 2: Elevation acquisition")
        job.add_log(f"Downloading elevation data ({primary_country})...")

        # Compute per-axis heightmap dimensions from bbox aspect ratio.
        # Enfusion supports non-square terrain (TerrainGridSizeX ≠ TerrainGridSizeZ).
        # The longest geographic axis gets the user's selected vertex count;
        # the shorter axis is proportional and snapped to valid Enfusion size.
        from config.enfusion import snap_to_enfusion_size
        from services.utils.geo import estimate_bbox_dimensions_m

        user_vertices = job.options.get("heightmap_size", 2048)
        user_vertices = snap_to_enfusion_size(user_vertices)

        width_m, height_m = estimate_bbox_dimensions_m(bbox)

        if width_m >= height_m:
            target_size_x = user_vertices
            target_size_z = snap_to_enfusion_size(round(user_vertices * (height_m / width_m)))
        else:
            target_size_z = user_vertices
            target_size_x = snap_to_enfusion_size(round(user_vertices * (width_m / height_m)))

        target_size = (target_size_x, target_size_z)
        logger.info(
            f"[{job.job_id}] Terrain dimensions: {target_size_x}x{target_size_z} vertices "
            f"(bbox {width_m:.0f}m x {height_m:.0f}m)"
        )
        job.add_log(
            f"Terrain dimensions: {target_size_x}x{target_size_z} vertices "
            f"(area {width_m:.0f}m x {height_m:.0f}m)"
        )

        # Pass the heightmap size to the elevation fetcher so it can limit
        # the output resolution.  Fetching at the native sensor resolution
        # (1 m → 15 000 px per axis for a 15 km area) then down-sampling to
        # the actual heightmap size (e.g. 2049) wastes ~1 GB of RAM during
        # the merge step.  We cap elevation to 2× the heightmap vertices so
        # there is still quality headroom for the later bicubic resample.
        max_vertex = max(target_size_x, target_size_z)
        elevation_result = await step_fetch_elevation(
            bbox, primary_country, job, max_pixels=max_vertex * 2,
        )

        job.progress = 25
        job.steps_completed.append({
            "step": "elevation_download",
            "source": elevation_result["source"],
            "resolution_m": elevation_result["resolution_m"],
        })
        logger.info(f"[{job.job_id}] Elevation: {elevation_result['source']} ({elevation_result['resolution_m']}m)")
        job.add_log(
            f"Downloaded elevation data from {elevation_result['source']} ({elevation_result['resolution_m']}m resolution)",
            "success"
        )

        # Step 3: Fetch OSM features (25% -> 40%)
        job.current_step = "Downloading map features (roads, water, forests, buildings)..."
        job.progress = 25
        logger.info(f"[{job.job_id}] Step 3: OSM feature extraction")
        job.add_log("Fetching map features (roads, water, forests, buildings) from OpenStreetMap...")

        osm_data = await step_fetch_osm(bbox, output_dir, job=job)

        job.progress = 40
        feature_counts = {k: len(v.get("features", [])) for k, v in osm_data.items()}
        job.steps_completed.append({"step": "osm_features", "feature_counts": feature_counts})
        logger.info(f"[{job.job_id}] OSM features: {feature_counts}")
        job.add_log(
            f"Fetched OSM features: {feature_counts.get('roads', 0)} roads, "
            f"{feature_counts.get('water', 0)} water features, "
            f"{feature_counts.get('forests', 0)} forests, "
            f"{feature_counts.get('buildings', 0)} buildings",
            "success"
        )

        # Step 4: Generate heightmap (40% -> 60%)
        job.current_step = "Generating heightmap..."
        job.progress = 40
        logger.info(f"[{job.job_id}] Step 4: Heightmap generation")
        job.add_log("Generating heightmap from elevation data...")

        # target_size already set in step 2 (from job.options["heightmap_size"])
        target_resolution = job.options.get("grid_resolution", 2.0)

        from services.heightmap_generator import ElevationTruncatedError

        try:
            heightmap_result = step_generate_heightmap(
                dem_bytes=elevation_result["data"],
                osm_data=osm_data,
                target_size=target_size,
                target_resolution=target_resolution,
                output_dir=output_dir,
                job=job,
            )
        except ElevationTruncatedError as e:
            # The national WCS source returned truncated data.
            # Fall back to OpenTopography Copernicus DEM 30m.
            original_source = elevation_result.get("source", "unknown")
            logger.warning(
                f"[{job.job_id}] Elevation data from {original_source} was truncated, "
                f"falling back to OpenTopography 30m: {e}"
            )
            job.add_log(
                f"Elevation data from {original_source} was truncated. "
                f"Falling back to OpenTopography Copernicus DEM 30m...",
                "warning"
            )
            job.progress = 42

            from services.elevation_service import fetch_elevation_opentopography
            fallback_data = await fetch_elevation_opentopography(bbox, "COP30")
            if not fallback_data:
                raise RuntimeError(
                    f"Elevation data from {original_source} was truncated, "
                    f"and OpenTopography fallback also failed."
                )

            elevation_result["data"] = fallback_data
            elevation_result["source"] = "Copernicus DEM GLO-30 (OpenTopography, fallback)"
            elevation_result["resolution_m"] = 30
            elevation_result["crs"] = "EPSG:4326"
            job.add_log("Downloaded fallback elevation from OpenTopography (30m)", "success")

            heightmap_result = step_generate_heightmap(
                dem_bytes=fallback_data,
                osm_data=osm_data,
                target_size=target_size,
                target_resolution=target_resolution,
                output_dir=output_dir,
                job=job,
            )
            job.add_log(
                f"Heightmap generated using OpenTopography 30m fallback",
                "warning"
            )

        # Free raw GeoTIFF bytes from elevation_result — no longer needed.
        # For Sweden STAC this can be ~192 MB. The heightmap pipeline has
        # already extracted the numpy array, so we only keep the metadata.
        elevation_result["data"] = None

        job.progress = 60
        job.steps_completed.append({
            "step": "heightmap",
            "dimensions": heightmap_result["dimensions"],
            "terrain_size": heightmap_result["terrain_size_m"],
            "elevation_range": f"{heightmap_result['min_elevation']:.1f}m - {heightmap_result['max_elevation']:.1f}m",
        })
        logger.info(
            f"[{job.job_id}] Heightmap generated: {heightmap_result['dimensions']} pixels, "
            f"{heightmap_result['terrain_size_m']}m terrain, "
            f"elevation range: {heightmap_result['min_elevation']:.1f}m - {heightmap_result['max_elevation']:.1f}m"
        )
        job.add_log(
            f"Generated heightmap: {heightmap_result['dimensions']} ({heightmap_result['terrain_size_m']}m terrain), "
            f"elevation range: {heightmap_result['min_elevation']:.1f}m - {heightmap_result['max_elevation']:.1f}m",
            "success"
        )

        # Step 5: Generate surface masks (60% -> 75%)
        # Reuses the elevation array from step 4 — no DEM re-parsing needed.
        job.current_step = "Generating surface masks..."
        job.progress = 60
        logger.info(f"[{job.job_id}] Step 5: Surface mask generation")
        job.add_log("Generating surface masks (9 types: grass, forest, pine, asphalt, gravel, dirt, rock, sand, water edge)...")

        # Pass heightmap dimensions so masks are generated at matching size
        hm_dims_str = heightmap_result["dimensions"]
        hm_dims_parts = hm_dims_str.split("x")
        heightmap_dims = (int(hm_dims_parts[0]), int(hm_dims_parts[1]))

        surface_result = step_generate_surface_masks(
            elevation_array=heightmap_result["_elevation_array"],
            osm_data=osm_data,
            bbox=bbox,
            target_resolution=target_resolution,
            output_dir=output_dir,
            primary_country=primary_country,
            heightmap_dimensions=heightmap_dims,
            job=job,
        )

        job.progress = 75
        job.steps_completed.append({
            "step": "surface_masks",
            "mask_count": surface_result["mask_count"],
            "surfaces": surface_result["surfaces"],
        })
        logger.info(
            f"[{job.job_id}] Surface masks: {surface_result['mask_count']} masks "
            f"({', '.join(surface_result['surfaces'])})"
        )
        job.add_log(
            f"Created {surface_result['mask_count']} surface masks: {', '.join(surface_result['surfaces'])}",
            "success"
        )

        # Step 6: Fetch satellite imagery (75% -> 77%)
        job.current_step = "Downloading satellite imagery..."
        job.progress = 75
        logger.info(f"[{job.job_id}] Step 6: Satellite imagery")
        if primary_country == "SE":
            job.add_log("Downloading satellite imagery (trying Lantmäteriet historical orthophotos, then Sentinel-2)...")
        else:
            job.add_log("Downloading satellite imagery from Sentinel-2 Cloudless...")

        satellite_result = await step_fetch_satellite_imagery(
            bbox=bbox,
            target_size_x=target_size_x,
            target_size_z=target_size_z,
            output_dir=output_dir,
            country_codes=country_info.get("countries", []),
            job=job,
        )

        job.progress = 77
        sat_source = satellite_result.get("source", "Sentinel-2 Cloudless")
        if satellite_result["success"]:
            job.steps_completed.append({
                "step": "satellite_imagery",
                "file": satellite_result["file"],
                "dimensions": satellite_result["dimensions"],
                "source": sat_source,
            })
            logger.info(f"[{job.job_id}] Satellite imagery: {satellite_result['file']} ({satellite_result['dimensions']}) from {sat_source}")
            job.add_log(
                f"Downloaded satellite imagery from {sat_source}: {satellite_result['file']} ({satellite_result['dimensions']})",
                "success"
            )
        else:
            logger.warning(f"[{job.job_id}] Satellite imagery download failed, continuing without it")
            job.add_log("Satellite imagery download failed (continuing without it)", "warning")

        # Step 7: Coordinate transformation setup (77% -> 78%)
        job.current_step = "Setting up coordinate transformation..."
        job.progress = 77
        logger.info(f"[{job.job_id}] Step 7: Coordinate transformation")

        from services.coordinate_transformer import CoordinateTransformer

        terrain_size_m = (
            float(heightmap_result["terrain_size_m"].split("x")[0]),
            float(heightmap_result["terrain_size_m"].split("x")[1]),
        )
        transformer = CoordinateTransformer(
            bbox=bbox,
            crs=country_info["crs"],
            terrain_size_m=terrain_size_m,
        )
        coord_verification = transformer.get_verification_data()
        job.add_log(
            f"Coordinate transform: {coord_verification['method']} ({country_info['crs']}), "
            f"projected {coord_verification['projected_width_m']:.0f}m x {coord_verification['projected_depth_m']:.0f}m",
            "success"
        )
        job.steps_completed.append({
            "step": "coordinate_transform",
            "method": coord_verification["method"],
            "crs": country_info["crs"],
        })

        # Step 8: Process roads (78% -> 82%)
        job.current_step = "Processing road network..."
        job.progress = 78
        logger.info(f"[{job.job_id}] Step 8: Road processing")
        job.add_log("Processing road network and classifying road types...")

        road_result = step_process_roads(
            osm_data, primary_country, output_dir,
            transformer=transformer,
            elevation_array=heightmap_result.get("_elevation_array"),
            job=job,
        )

        job.progress = 82
        road_stats = road_result.get("stats", {})
        road_total = road_stats.get("total", 0)
        road_by_surface = road_stats.get("by_surface", {})
        job.steps_completed.append({
            "step": "road_processing",
            "road_count": road_total,
            "by_surface": road_by_surface,
        })
        surface_breakdown = ", ".join([f"{k}: {v}" for k, v in road_by_surface.items()])
        logger.info(
            f"[{job.job_id}] Roads: {road_total} segments "
            f"({surface_breakdown})"
        )
        job.add_log(
            f"Processed {road_total} road segments. By surface: {surface_breakdown}",
            "success"
        )

        # Step 9: Extract features (82% -> 86%)
        job.current_step = "Extracting map features..."
        job.progress = 82
        logger.info(f"[{job.job_id}] Step 9: Feature extraction")
        job.add_log("Extracting water bodies, forests, and building details...")

        features = step_extract_features(osm_data, primary_country, output_dir, job)

        job.progress = 86
        job.steps_completed.append({"step": "feature_extraction", "summary": features["summary"]})
        logger.info(f"[{job.job_id}] Features: {features['summary']}")
        summary = features["summary"]
        job.add_log(
            f"Extracted features: {summary.get('lakes', 0)} lakes, {summary.get('rivers', 0)} rivers, "
            f"{summary.get('forest_areas', 0)} forests, {summary.get('buildings', 0)} buildings",
            "success"
        )

        # Step 10: Build metadata (86% -> 87%)
        job.current_step = "Building metadata..."
        job.progress = 86

        map_name = job.options.get("map_name", "")
        if not map_name:
            # Auto-generate from country + approximate coordinates
            center_lat = (bbox["south"] + bbox["north"]) / 2
            center_lon = (bbox["west"] + bbox["east"]) / 2
            map_name = f"{primary_country}_{abs(center_lat):.0f}{'N' if center_lat >= 0 else 'S'}_{abs(center_lon):.0f}{'E' if center_lon >= 0 else 'W'}"

        metadata = build_metadata(
            polygon_coords=job.polygon_coords,
            options=job.options,
            country_info=country_info,
            elevation_result=elevation_result,
            heightmap_result=heightmap_result,
            surface_result=surface_result,
            road_result=road_result,
            features=features,
            satellite_result=satellite_result,
            coordinate_transform=coord_verification,
            map_name=map_name,
        )

        with open(output_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        # Step 11: Generate Enfusion project files (87% -> 92%)
        job.current_step = "Generating Enfusion project files..."
        job.progress = 87
        logger.info(f"[{job.job_id}] Step 11: Enfusion project generation")
        job.add_log("Generating Enfusion Workbench project files...")

        from services.enfusion_project_generator import EnfusionProjectGenerator, sanitize_project_name

        sanitized_name = sanitize_project_name(map_name)
        enfusion_gen = EnfusionProjectGenerator(
            map_name=sanitized_name,
            metadata=metadata,
            road_data=road_result,
            transformer=transformer,
            elevation_array=heightmap_result.get("_elevation_array"),
        )
        enfusion_files = enfusion_gen.generate_all(output_dir, job=job)

        job.progress = 92
        job.add_log(
            f"Generated {len(enfusion_files)} Enfusion project files (addon.gproj, world, layers, mission)",
            "success"
        )
        job.steps_completed.append({
            "step": "enfusion_project",
            "map_name": sanitized_name,
            "files_created": len(enfusion_files),
        })

        # Step 12: Generate SETUP_GUIDE.md (92% -> 93%)
        job.current_step = "Generating setup guide..."
        job.progress = 92
        logger.info(f"[{job.job_id}] Step 12: Setup guide generation")

        from services.setup_guide_generator import SetupGuideGenerator

        guide_gen = SetupGuideGenerator(sanitized_name, metadata)
        guide_gen.generate(output_dir)
        job.add_log("Generated comprehensive SETUP_GUIDE.md", "success")
        job.steps_completed.append({
            "step": "setup_guide",
        })

        # Step 13: Organize export and create ZIP (93% -> 95%)
        job.current_step = "Organizing export and creating ZIP..."
        job.progress = 93
        logger.info(f"[{job.job_id}] Step 13: Export packaging")
        job.add_log("Organizing files into Enfusion project structure...")

        from services.export_service import organize_export_structure
        organize_export_structure(output_dir, sanitized_name, job=job)

        job.progress = 95
        job.add_log("Creating ZIP archive...")
        zip_path = OUTPUT_DIR / f"map_{job.job_id}"
        shutil.make_archive(str(zip_path), "zip", output_dir)

        # Verify ZIP file exists before marking as completed
        zip_file = OUTPUT_DIR / f"map_{job.job_id}.zip"
        if not zip_file.exists():
            raise RuntimeError("ZIP file was not created successfully")

        job.add_log(f"ZIP file created: {zip_file.stat().st_size / (1024*1024):.1f} MB", "success")
        job.steps_completed.append({
            "step": "export_organized",
        })

        # Done
        job.progress = 100
        job.status = "completed"
        job.current_step = "Generation complete!"
        job.completed_at = datetime.utcnow().isoformat()
        job.result = {
            "output_dir": str(output_dir),
            "zip_file": f"map_{job.job_id}.zip",
            "metadata": metadata,
            "files": [f.name for f in output_dir.iterdir() if f.is_file()],
        }

        logger.info(f"[{job.job_id}] Generation complete! Output: {output_dir}")
        job.add_log("Map generation completed successfully!", "success")

        # Schedule cleanup now that generation is complete
        # This gives users the full retention time from completion, not from job creation
        from main import schedule_cleanup, FILE_RETENTION_MINUTES
        asyncio.create_task(schedule_cleanup(job.job_id, FILE_RETENTION_MINUTES))

    except Exception as e:
        logger.exception(f"[{job.job_id}] Generation failed: {e}")
        job.status = "failed"
        job.current_step = f"Error: {str(e)}"
        job.errors.append(str(e))
