"""
Export service for packaging generated map data.

Handles creating downloadable archives and serving files.
"""

import logging
import shutil
from pathlib import Path
from typing import Optional

from config import OUTPUT_DIR

logger = logging.getLogger(__name__)


def get_output_zip(job_id: str, output_dir: Path = OUTPUT_DIR) -> Optional[Path]:
    """
    Get the path to the output ZIP file for a job.

    Args:
        job_id: Job identifier
        output_dir: Base output directory

    Returns:
        Path to ZIP file or None if not found
    """
    zip_path = output_dir / f"map_{job_id}.zip"
    if zip_path.exists():
        return zip_path
    return None


def get_preview_image(job_id: str, output_dir: Path = OUTPUT_DIR, image_type: str = "heightmap") -> Optional[Path]:
    """
    Get preview image path for a job.

    Args:
        job_id: Job identifier
        output_dir: Base output directory
        image_type: Type of preview ("heightmap", "surface", "roads")

    Returns:
        Path to preview image or None
    """
    job_dir = output_dir / job_id
    if not job_dir.exists():
        return None

    preview_map = {
        "heightmap": "heightmap_preview.png",
        "surface": "surface_preview.png",
    }

    filename = preview_map.get(image_type)
    if filename:
        # Check direct path first, then Sourcefiles/ subdirectory
        path = job_dir / filename
        if path.exists():
            return path
        path_sub = job_dir / "Sourcefiles" / filename
        if path_sub.exists():
            return path_sub

    return None


def cleanup_job(job_id: str, output_dir: Path = OUTPUT_DIR):
    """
    Remove all output files for a completed job.

    Thread-safe with error handling to prevent issues during concurrent access.
    """
    job_dir = output_dir / job_id
    zip_path = output_dir / f"map_{job_id}.zip"

    cleaned = False

    try:
        if job_dir.exists():
            shutil.rmtree(job_dir)
            cleaned = True
            logger.debug(f"Removed job directory for {job_id[:8]}...")
    except Exception as e:
        logger.error(f"Failed to remove job directory for {job_id[:8]}...: {e}")

    try:
        if zip_path.exists():
            zip_path.unlink()
            cleaned = True
            logger.debug(f"Removed ZIP file for {job_id[:8]}...")
    except Exception as e:
        logger.error(f"Failed to remove ZIP file for {job_id[:8]}...: {e}")

    if cleaned:
        logger.info(f"Cleaned up job {job_id[:8]}...")
    else:
        logger.debug(f"No files to clean up for job {job_id[:8]}...")


def list_job_files(job_id: str, output_dir: Path = OUTPUT_DIR) -> list[dict]:
    """List all output files for a job."""
    job_dir = output_dir / job_id
    if not job_dir.exists():
        return []

    files = []
    for path in sorted(job_dir.iterdir()):
        if path.is_file():
            files.append({
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "size_human": _human_readable_size(path.stat().st_size),
            })

    return files


def _human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def organize_export_structure(output_dir: Path, map_name: str, job=None):
    """
    Organize flat output files into the Enfusion project folder structure.

    Moves files into Sourcefiles/ and Reference/ subdirectories so the
    export ZIP has the correct structure:

      <MapName>/
        addon.gproj
        Worlds/
        Missions/
        Sourcefiles/    <- heightmaps, surface masks, satellite
        Reference/      <- GeoJSON, CSV, metadata
        SETUP_GUIDE.md

    Args:
        output_dir: The job output directory containing all generated files.
        map_name: Sanitized project name.
    """
    sourcefiles_dir = output_dir / "Sourcefiles"
    reference_dir = output_dir / "Reference"
    sourcefiles_dir.mkdir(exist_ok=True)
    reference_dir.mkdir(exist_ok=True)

    # Count total files to organize
    n_files = sum(1 for f in output_dir.iterdir() if f.is_file())
    if job:
        job.add_log(f"Organizing {n_files} files into Enfusion project structure...")

    # Files to move to Sourcefiles/
    sourcefiles_patterns = [
        "heightmap.asc",
        "heightmap.png",
        "heightmap_preview.png",
        "satellite_map.png",
        "surface_grass.png",
        "surface_forest_floor.png",
        "surface_pine_floor.png",
        "surface_asphalt.png",
        "surface_gravel.png",
        "surface_dirt.png",
        "surface_rock.png",
        "surface_sand.png",
        "surface_water_edge.png",
        "surface_preview.png",
    ]

    # Files to move to Reference/
    reference_patterns = [
        "roads_enfusion.geojson",
        "roads_enfusion_local.geojson",
        "roads_splines.csv",
        "roads_reference.csv",
        "features.json",
        "metadata.json",
    ]

    # Also move any osm_*.geojson files to Reference/
    osm_files = [
        f.name for f in output_dir.iterdir()
        if f.is_file() and f.name.startswith("osm_") and f.name.endswith(".geojson")
    ]
    reference_patterns.extend(osm_files)

    def _safe_move(src: Path, dst_dir: Path):
        """Move file to directory if it exists, skip silently otherwise."""
        if src.exists() and src.is_file():
            dst = dst_dir / src.name
            if not dst.exists():  # Don't overwrite if already in place
                shutil.move(str(src), str(dst))
                logger.debug(f"Moved {src.name} -> {dst_dir.name}/")

    for pattern in sourcefiles_patterns:
        _safe_move(output_dir / pattern, sourcefiles_dir)

    for pattern in reference_patterns:
        _safe_move(output_dir / pattern, reference_dir)

    # Remove old IMPORT_GUIDE.md if it exists (replaced by SETUP_GUIDE.md)
    old_guide = output_dir / "IMPORT_GUIDE.md"
    if old_guide.exists():
        old_guide.unlink()

    logger.info(f"Organized export structure for {map_name}")


def generate_import_guide(output_dir: Path, metadata: dict):
    """Generate a step-by-step Enfusion World Editor import guide (legacy)."""
    settings = metadata["enfusion_import"]["recommended_settings"]

    guide = f"""# Arma Reforger World Editor Import Guide

## Generated Map Information
- **Countries**: {', '.join(metadata['input']['countries'])}
- **Terrain Size**: {metadata['heightmap']['terrain_size_m']}
- **Grid Cell Size**: {metadata['heightmap']['grid_cell_size_m']}m
- **Elevation Range**: {metadata['elevation']['min_elevation_m']:.1f}m - {metadata['elevation']['max_elevation_m']:.1f}m
- **Elevation Source**: {metadata['elevation']['source']}

## Files Included
- `heightmap.asc` - Heightmap (ESRI ASCII Grid format, preferred)
- `heightmap.png` - Heightmap (16-bit PNG, alternative)
- `heightmap_preview.png` - Heightmap preview image
- `satellite_map.png` - Satellite imagery (Sentinel-2 Cloudless)
- `surface_*.png` - Surface material masks (8-bit grayscale)
- `surface_preview.png` - Combined surface preview
- `roads_enfusion.geojson` - Road data with Enfusion prefab mapping
- `roads_splines.csv` - Road spline control points
- `osm_*.geojson` - Raw OSM data (roads, water, forests, buildings, land use)
- `features.json` - Processed feature data
- `metadata.json` - Full generation metadata

## Import Steps

### 1. Create New Terrain
1. Open Arma Reforger World Editor
2. Go to **World** → **Terrain** → **Create Terrain**
3. Set terrain parameters:
   - **Grid Cell Size**: {settings['grid_cell_size']}m
   - **Height Scale**: {settings['height_scale']:.6f}
   - **Height Offset**: {settings['height_offset']:.1f}

### 2. Import Heightmap
1. In Terrain Editor, select **Import Heightmap**
2. Choose `heightmap.asc` (recommended) or `heightmap.png`
3. Settings:
   - Invert X Axis: {'Yes' if settings['invert_x_axis'] else 'No'}
   - Invert Z Axis: {'Yes' if settings['invert_z_axis'] else 'No'}
4. Click **Import** and verify terrain shape

### 3. Import Surface Masks
1. In Terrain Editor, go to **Surface Painting**
2. For each surface mask:
   - Select the target surface material
   - Import the corresponding `surface_*.png` mask
3. Surface mapping:
   - `surface_grass.png` → Grass material
   - `surface_forest_floor.png` → Forest floor (deciduous)
   - `surface_pine_floor.png` → Forest floor (coniferous)
   - `surface_asphalt.png` → Asphalt / Concrete
   - `surface_gravel.png` → Gravel
   - `surface_dirt.png` → Dirt / Farmland
   - `surface_rock.png` → Rock
   - `surface_sand.png` → Sand / Seabed
   - `surface_water_edge.png` → Mud / Water edge

### 4. Place Roads
1. Open `roads_enfusion.geojson` for reference
2. For each road segment:
   - Create a **SplineShapeEntity**
   - Set the road prefab (listed in the GeoJSON properties)
   - Place control points along the road path
   - Enable **Adjust Height Map** for terrain alignment
3. Alternative: Use `roads_splines.csv` for scripted import

### 5. Place Water Bodies
- Reference `features.json` for lake and river data
- Create **WaterEntity** for each lake
- Create **WaterFlowEntity** for rivers

### 6. Place Vegetation
- Reference `features.json` for forest areas
- Create **ForestGeneratorEntity** for each forest zone
- Set tree density and species from the forest data

### 7. Place Buildings
- Reference `features.json` for building locations
- Place building prefabs at specified coordinates

### 8. Use Satellite Map as Reference
- `satellite_map.png` provides real-world imagery of the terrain
- Use it as a visual reference when:
  - Verifying surface mask placement matches reality
  - Identifying additional features not captured in OSM
  - Validating road network accuracy
  - Planning vegetation and object placement
- The satellite image aligns with the terrain bounds and can be overlaid for comparison

## Data Sources Used
- **Elevation**: {metadata['elevation']['source']} ({metadata['elevation']['resolution_m']}m resolution)
- **Satellite Imagery**: {metadata.get('satellite', {}).get('source', 'Not available')}
- **Roads**: OpenStreetMap ({metadata['roads']['total_segments']} segments)
- **Features**: OpenStreetMap via Overpass API

## Notes
- Road data in `roads_splines.csv` uses WGS84 geographic coordinates (longitude, latitude)
- Transform coordinates to match your terrain origin point
- Surface masks may need adjustment for optimal visual quality
- Forest density values are estimates; adjust in World Editor as needed
"""

    with open(output_dir / "IMPORT_GUIDE.md", "w") as f:
        f.write(guide)
