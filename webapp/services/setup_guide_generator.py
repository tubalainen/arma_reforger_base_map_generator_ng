"""
Comprehensive Enfusion Workbench setup guide generator.

Generates a personalised, step-by-step SETUP_GUIDE.md with:
- Pre-computed terrain values (no calculations for the user)
- Exact UI paths in Enfusion Workbench
- Verification checkpoints after each phase
- Contextual warnings (save+reload, block saturation, etc.)
- Coverage statistics and recommended defaults
- Troubleshooting section

Replaces the previous bare-bones IMPORT_GUIDE.md.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config.enfusion import (
    SURFACE_MATERIAL_MAP,
    SURFACE_MATERIAL_ALTERNATIVES,
    SURFACE_IMPORT_ORDER,
    DEFAULT_ADDON_DIR,
)

logger = logging.getLogger(__name__)


class SetupGuideGenerator:
    """
    Generates a comprehensive, context-aware SETUP_GUIDE.md.

    All values in the guide are pre-computed from generation metadata —
    the user never needs to calculate anything themselves.
    """

    def __init__(self, map_name: str, metadata: dict):
        """
        Initialize the guide generator.

        Args:
            map_name: Sanitized map/project name.
            metadata: Full generation metadata dict (same as metadata.json).
        """
        self.map_name = map_name
        self.metadata = metadata

        # Extract commonly used values
        self.hm = metadata.get("heightmap", {})
        self.elev = metadata.get("elevation", {})
        self.surf = metadata.get("surface_masks", {})
        self.roads = metadata.get("roads", {})
        self.features = metadata.get("features", {})
        self.satellite = metadata.get("satellite", {})
        self.input_data = metadata.get("input", {})
        self.enfusion = metadata.get("enfusion_import", {})
        self.settings = self.enfusion.get("recommended_settings", {})
        self.coord_info = metadata.get("coordinate_transform", {})

        # Coverage data
        self.coverage = self.surf.get("coverage", {})
        self.coverage_per_surface = self.coverage.get("per_surface", {})
        self.recommended_default = self.coverage.get("recommended_default", "grass")
        self.default_material = self.coverage.get(
            "recommended_default_material",
            SURFACE_MATERIAL_MAP.get("grass", "Grass_01.emat"),
        )

    def generate(self, output_dir: Path) -> Path:
        """
        Generate the full SETUP_GUIDE.md.

        Args:
            output_dir: Directory to write the guide into.

        Returns:
            Path to the generated guide file.
        """
        sections = [
            self._header(),
            self._quick_reference(),
            self._prerequisites(),
            self._phase_project_setup(),
            self._phase_terrain_creation(),
            self._phase_surface_painting(),
            self._phase_satellite_map(),
            self._phase_roads(),
            self._phase_vegetation_water(),
            self._phase_testing(),
            self._known_limitations(),
            self._appendix_files(),
            self._appendix_parameters(),
            self._appendix_troubleshooting(),
            self._appendix_next_steps(),
        ]

        content = "\n\n".join(sections)

        guide_path = output_dir / "SETUP_GUIDE.md"
        with open(guide_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Generated SETUP_GUIDE.md: {guide_path}")
        return guide_path

    # -----------------------------------------------------------------------
    # Section generators
    # -----------------------------------------------------------------------

    def _header(self) -> str:
        return f"# {self.map_name} — Enfusion Workbench Setup Guide"

    def _quick_reference(self) -> str:
        terrain_size = self.hm.get("terrain_size_m", "unknown")
        dims = self.hm.get("dimensions", "unknown")
        cell_size = self.hm.get("grid_cell_size_m", 2.0)
        min_elev = self.elev.get("min_elevation_m", 0)
        max_elev = self.elev.get("max_elevation_m", 0)
        height_scale = self.elev.get("height_scale", 0.03125)
        mask_count = self.surf.get("count", 5)
        road_count = self.roads.get("total_segments", 0)
        countries = ", ".join(self.input_data.get("countries", ["Unknown"]))
        crs = self.input_data.get("crs", "Unknown")

        block_violations = self.surf.get("block_saturation", {}).get("violations", 0)
        total_blocks = self.surf.get("block_saturation", {}).get("total_blocks", 0)

        return f"""## Quick Reference Card

| Property | Value |
|----------|-------|
| **Terrain Size** | {terrain_size} |
| **Heightmap** | {dims} pixels |
| **Grid Cell Size** | {cell_size}m |
| **Height Range** | {min_elev:.1f}m to {max_elev:.1f}m |
| **Height Scale** | {height_scale:.6g} |
| **Surface Masks** | {mask_count} included |
| **Recommended Default** | {self.recommended_default} ({self.default_material}) |
| **Block Violations** | {block_violations}/{total_blocks} blocks |
| **Road Segments** | {road_count} |
| **Region** | {countries} ({crs}) |
| **Estimated Setup Time** | ~30-45 minutes |"""

    def _prerequisites(self) -> str:
        return """## Prerequisites

- **Arma Reforger Tools** installed via Steam (free DLC)
- At least **8 GB RAM** recommended for terrain operations
- **Do NOT** place the project folder inside a OneDrive directory — it will fail to load"""

    def _phase_project_setup(self) -> str:
        return f"""## Phase 1: Project Setup (5 minutes)

### Step 1.1: Copy Project Folder

Copy the entire **`{self.map_name}/`** folder from this ZIP to your Arma Reforger Workbench addons directory:

```
{DEFAULT_ADDON_DIR}\\{self.map_name}\\
```

You should see this structure inside:
```
{self.map_name}/
  addon.gproj
  Worlds/
  Missions/
  Sourcefiles/
  Reference/
  SETUP_GUIDE.md    (this file)
```

### Step 1.2: Open in Enfusion Workbench

1. Launch **Arma Reforger Tools** from Steam
2. In the Workbench launcher, click **Add Project** > **Add Existing Project**
3. Navigate to `{DEFAULT_ADDON_DIR}\\{self.map_name}\\addon.gproj`
4. Click **Open**
5. The project should appear in the Projects list with the name **"{self.map_name}"**

### Step 1.3: Open the World

1. In the **Resource Browser** (bottom panel), navigate to `Worlds/`
2. Double-click **`{self.map_name}.ent`** to open the world
3. The World Editor should open without errors

> **Note**: The world does not contain a terrain yet — you will create it in the next phase.
> You should see an empty world with sky and lighting. If you see error messages, check
> that ArmaReforger is listed as a dependency in the Projects panel."""

    def _phase_terrain_creation(self) -> str:
        dims = self.hm.get("dimensions", "2049x2049")
        parts = dims.split("x")
        vertex_x = int(parts[0])
        vertex_z = int(parts[1]) if len(parts) > 1 else vertex_x
        face_x = vertex_x - 1
        face_z = vertex_z - 1
        cell_size = self.hm.get("grid_cell_size_m", 2.0)
        height_scale = self.elev.get("height_scale", 0.03125)

        return f"""## Phase 2: Terrain Creation (10 minutes)

### Step 2.1: Create New Terrain

1. In the World Editor, select the **GenericTerrainEntity** in the hierarchy
   (it should already be at position 0, 0, 0)
2. Right-click the terrain entity > **Create new terrain...**
3. In the **New Terrain** dialog, enter these **exact** values:

| Parameter | Value |
|-----------|-------|
| **Name** | **{self.map_name}** |
| **Terrain grid size** | **{face_x}** = **{face_z}** |
| **Blocks per tile** | **4** (default) |
| **Grid cell size (meters)** | **{cell_size}** |
| **Height scale (meters)** | **{height_scale:.6g}** |
| **"Zero" height to entity coord** | **10%** (default) |
| **Surface layer weight bits** | **8 bits (5 surfaces)** (default) |

4. Verify the summary at the bottom of the dialog shows the expected terrain size
5. Click **Create**
6. Wait for the terrain to generate (may take a few seconds)

### Step 2.2: Import Heightmap

1. With the terrain entity selected, select the **Terrain Tool** or press _(Ctrl+T)_ to open the **Terrain Tool** panel
2. Go to the **Manage** tab
3. Click **Import Height Map...**
4. Navigate to: `{self.map_name}/Sourcefiles/heightmap.asc`
5. Settings:
   - **Invert X Axis**: No
   - **Invert Z Axis**: Yes
6. Click **Import**

> **CRITICAL**: After import, you **MUST** save and reopen the world:
> 1. **File > Save World** (Ctrl+S)
> 2. Wait for the save to complete (watch the progress bar in the upper right corner)
> 3. Close and reopen the world (double-click the .ent file again in the Resource Browser)

### Step 2.3: Verify Terrain Shape

After reopening, you should see your terrain with real-world elevation:
- Mountains/hills at the correct positions
- Valleys and flat areas

> **Tip**: Use the **heightmap_preview.png** in Sourcefiles/ as a visual reference
> to verify the terrain shape matches expectations. You may need to navigate the
> camera to see the terrain — try pressing **F** to focus on the selected entity.

> **Note**: If a **"Set normal map options"** dialog appears, click **OK** to accept
> the defaults."""

    def _phase_surface_painting(self) -> str:
        lines = [f"""## Phase 3: Surface Painting (15 minutes)

This is the most important phase. Your terrain currently has a default grey surface.
We'll import pre-generated surface masks to paint it with realistic materials.

### Step 3.1: Open Paint Tool

1. With the terrain entity selected, open the **Terrain Tool** _(Ctrl+T)_
2. Switch to the **Paint** tab
3. You'll see a surface layer list on the right side

### Step 3.2: Set Default Surface

The default surface covers 100% of your terrain as the base layer.

1. The first surface in the Paint panel is the default — it cannot be removed
2. Right-click it > **Change layer's material...**
3. In the Resource Browser, navigate to:
   `{self.default_material}`
4. Select it and click **OK**

> **Why {self.recommended_default}?** Your terrain is {self.coverage_per_surface.get(self.recommended_default, {}).get('percentage', 'N/A')}% {self.recommended_default},
> making it the optimal default surface."""]

        # Step 3.3: Add surface materials
        lines.append("""### Step 3.3: Add Surface Materials

For each surface mask, you need to add the corresponding material to the Paint panel:""")

        for i, surface_name in enumerate(SURFACE_IMPORT_ORDER, 1):
            material = SURFACE_MATERIAL_MAP.get(surface_name, "Unknown.emat")
            pct = self.coverage_per_surface.get(surface_name, {}).get("percentage", "?")
            alternatives = SURFACE_MATERIAL_ALTERNATIVES.get(surface_name, [])
            alt_str = f" (alternatives: {', '.join(a.split('/')[-1] for a in alternatives)})" if alternatives else ""

            lines.append(f"""
{i}. Drag **`{material}`** from the Resource Browser into the surface layer list{alt_str}""")

        # Step 3.4: Import masks
        lines.append("""
### Step 3.4: Import Surface Masks

Import masks in this specific order (most specific surfaces first):

> **Batch Import Option**: Right-click in the surface list > **Batch import surface masks**
> to import all masks at once. If using batch import, ensure files are named to match
> the surface materials. Otherwise, import individually:""")

        for i, surface_name in enumerate(SURFACE_IMPORT_ORDER, 1):
            material = SURFACE_MATERIAL_MAP.get(surface_name, "Unknown.emat")
            material_short = material.split("/")[-1].replace(".emat", "")
            pct = self.coverage_per_surface.get(surface_name, {}).get("percentage", "?")

            verification = {
                "rock": "Mountain peaks and steep slopes should now show rock texture",
                "forest_floor": "Forested areas should show dark earth/leaf litter texture",
                "asphalt": "Roads and urban areas should show paved surface",
                "sand_dirt": "Shorelines and farmland should show dirt/sand texture",
            }.get(surface_name, "Surface should be visible in the expected areas")

            lines.append(f"""
#### Step 3.4.{i}: Import {surface_name.replace('_', ' ').title()} ({pct}% coverage)

1. In the Paint tab, right-click **{material_short}** in the surface list
2. Select **Priority Surface Mask Import...**
3. Navigate to: `{self.map_name}/Sourcefiles/surface_{surface_name}.png`
4. Click **Open** — the mask will be applied
5. Verify: {verification}""")

        # Block saturation check
        block_violations = self.surf.get("block_saturation", {}).get("violations", 0)
        total_blocks = self.surf.get("block_saturation", {}).get("total_blocks", 0)

        lines.append("""
After importing all masks, **File > Save World** (Ctrl+S).""")

        lines.append(f"""
### Step 3.5: Verify Block Surface Limits

Your terrain has **{block_violations}** block saturation violations out of {total_blocks} total blocks.

{"All blocks are within the 5-surface limit. No action needed." if block_violations == 0 else f"There are {block_violations} blocks exceeding the 5-surface limit. Use the **Info & Diags** tab to identify and fix them."}

To check manually:
1. Switch to the **Info & Diags** tab in the Terrain Tool
2. Press **Ctrl+X** in the viewport to visualise surface layers
3. The **3x3 grid indicator** shows: Green = free slot, Yellow = selected, Red = limit reached
4. Use **Merge** to combine surfaces in saturated blocks""")

        return "\n".join(lines)

    def _phase_satellite_map(self) -> str:
        if not self.satellite.get("file"):
            return """## Phase 4: Satellite Map (Skipped)

No satellite imagery was available for this region. You can add satellite imagery
manually later via Terrain Tool (Ctrl+T) > Manage tab > Import Satellite Map."""

        return f"""## Phase 4: Satellite Map (5 minutes)

### Step 4.1: Import Satellite Image

1. With the terrain entity selected, open the **Terrain Tool** _(Ctrl+T)_
2. Go to the **Manage** tab
3. Click **Import Satellite Map...**
4. Navigate to: `{self.map_name}/Sourcefiles/satellite_map.png`
5. Click **Import**
6. **File > Save World** (Ctrl+S)

### Step 4.2: Verify Alignment

The satellite image should align with your terrain features:
- Roads visible in the satellite should match the terrain surface masks
- Water bodies should align with terrain low points
- Forest areas should match the forest floor surface mask

> **Source**: Sentinel-2 Cloudless imagery ({self.satellite.get('dimensions', 'unknown')} pixels)"""

    def _phase_roads(self) -> str:
        road_count = self.roads.get("total_segments", 0)

        if road_count == 0:
            return """## Phase 5: Roads (Skipped)

No roads were found in the selected area."""

        by_surface = self.roads.get("by_surface", {})
        surface_str = ", ".join(f"{k}: {v}" for k, v in by_surface.items())

        return f"""## Phase 5: Roads (Optional — Manual Placement)

Your terrain has **{road_count}** road segments ({surface_str}).

Road entities have been pre-generated in the **roads layer** (`{self.map_name}_roads.layer`).
These provide a starting framework but may need manual refinement.

### Step 5.1: Review Generated Roads

1. In the World Editor hierarchy, expand the **roads** layer
2. You should see SplineShapeEntity entries for each road
3. Each has a RoadGeneratorEntity child with the correct road prefab

### Step 5.2: Refine Roads

- Select a road entity to see its spline in the viewport
- Use the **Vector Tool** (V) to adjust spline points
- Roads with **"Adjust Height Map"** enabled will automatically flatten terrain
- Complex intersections may need manual cleanup

### Step 5.3: Reference Data

For manual road placement or verification:
- `Reference/roads_enfusion.geojson` — road data with local coordinates and prefab names
- `Reference/roads_splines.csv` — spline control points in local metres

> **Note**: Road prefabs are located at `Prefabs/WEGenerators/Roads/` in the ArmaReforger data."""

    def _phase_vegetation_water(self) -> str:
        lakes = self.features.get("lakes", 0)
        rivers = self.features.get("rivers", 0)
        forests = self.features.get("forest_areas", 0)

        return f"""## Phase 6: Vegetation & Water (Optional — Manual Placement)

### Step 6.1: Forest Generator

Your terrain has **{forests}** forest areas identified from OpenStreetMap data.

To add forests:
1. Create a **closed Spline** shape matching the forest boundary
2. Enable **Avoid Roads** and **Avoid Lakes** options
3. Use prefabs from `Prefabs/WEGenerators/Forest/` (prefixed `FG_`)
4. Drag the prefab onto the spline you created and wait (it generates a forest)

> **Optional reference**: `Reference/osm_forests.geojson` and `Reference/features.json`
> contain forest boundary data in Enfusion local coordinates for positioning guidance.

### Step 6.2: Water Bodies

Your terrain has **{lakes}** lakes and **{rivers}** rivers/streams.

To add lakes:
1. Create a **closed Spline** shape matching the lake boundary
2. Add a **Lake Generator** entity as a child
3. Use prefabs from `Prefabs/WEGenerators/Water/Lake/` (prefixed `LG_`)
4. Set **Flatten By Bottom Plane** for natural water level
5. Reference: `Reference/osm_water.geojson` and `Reference/features.json`

> **Tip**: Water body coordinates in features.json are already in Enfusion local metres."""

    def _phase_testing(self) -> str:
        return f"""## Phase 7: Testing (5 minutes)

### Step 7.1: Save Everything

1. **File > Save World** (Ctrl+S)
2. Ensure no unsaved changes in any layer

### Step 7.2: Play in Editor

1. Click the **Play** button in the toolbar (or press F5)
2. You should spawn in **Game Master** mode at the terrain centre
3. Use Game Master controls to fly around and inspect:
   - Terrain elevation and shape
   - Surface materials and transitions
   - Road placement (if applicable)
   - Lighting and atmosphere

### Step 7.3: Verify Checklist

- [ ] Terrain shape matches expected topography
- [ ] Surface materials look natural (grass, rock, forest floor visible)
- [ ] No obvious visual glitches or missing textures
- [ ] Roads follow correct paths (if placed)
- [ ] Sky, lighting, and fog look correct"""

    def _known_limitations(self) -> str:
        crs = self.input_data.get("crs", "Unknown")
        countries = self.input_data.get("countries", [])
        elev_source = self.elev.get("source", "Unknown")
        elev_res = self.elev.get("resolution_m", "Unknown")

        border_note = ""
        if len(countries) > 1:
            border_note = (
                f"\n- **Border area**: Your terrain spans {', '.join(countries)}. "
                f"Settings are optimised for the primary country. "
                f"Surfaces near the border may need minor adjustment."
            )

        return f"""## Known Limitations & Tips

### Data Accuracy
- **Elevation resolution**: Source data is {elev_source} ({elev_res}m resolution).
  Terrain features smaller than {elev_res}m may not be represented.
- **Map features**: Roads, forests, and buildings come from OpenStreetMap volunteer mapping.
  Coverage quality varies by region.{border_note}

### Enfusion Workbench Behaviour
- **Save after imports**: Always save your world (Ctrl+S) after importing heightmaps and
  surface masks. After the heightmap import, close and reopen the world to see the changes.
- **Performance**: Large terrains (4096+ faces) may take longer to load and edit.
  Consider reducing detail settings in the Workbench if performance is poor.

### Coordinate System
- Coordinates: **{crs}**
- All reference data uses Enfusion local metres (origin at terrain SW corner)."""

    def _appendix_files(self) -> str:
        return f"""## Appendix A: File Reference

### Project Files
| File | Purpose |
|------|---------|
| `addon.gproj` | Enfusion project definition |
| `Worlds/{self.map_name}.ent` | World file (layer index) |
| `Worlds/{self.map_name}_default.layer` | Terrain, lighting, atmosphere |
| `Worlds/{self.map_name}_managers.layer` | Camera, weather, audio managers |
| `Worlds/{self.map_name}_gamemode.layer` | Game Master mode (for testing) |
| `Worlds/{self.map_name}_roads.layer` | Pre-generated road entities |
| `Worlds/{self.map_name}_vegetation.layer` | Placeholder for forest generators |
| `Worlds/{self.map_name}_water.layer` | Placeholder for water entities |
| `Missions/{self.map_name}.conf` | Mission header (makes world playable) |

### Source Files (for import into Workbench)
| File | Purpose |
|------|---------|
| `Sourcefiles/heightmap.asc` | Primary heightmap (ESRI ASCII Grid) — **use this** |
| `Sourcefiles/heightmap.png` | Alternative heightmap (16-bit PNG) |
| `Sourcefiles/heightmap_preview.png` | Visual preview of elevation |
| `Sourcefiles/satellite_map.png` | Sentinel-2 satellite imagery |
| `Sourcefiles/surface_grass.png` | Surface mask: grass/meadow |
| `Sourcefiles/surface_forest_floor.png` | Surface mask: forest floor |
| `Sourcefiles/surface_asphalt.png` | Surface mask: paved areas |
| `Sourcefiles/surface_rock.png` | Surface mask: rock/slopes |
| `Sourcefiles/surface_sand_dirt.png` | Surface mask: sand/dirt |
| `Sourcefiles/surface_preview.png` | Combined surface preview |

### Reference Files (for manual placement)
| File | Purpose |
|------|---------|
| `Reference/roads_enfusion.geojson` | Road data with local coordinates |
| `Reference/roads_splines.csv` | Road spline points (local metres) |
| `Reference/features.json` | Lakes, rivers, forests, buildings |
| `Reference/metadata.json` | Full generation metadata |
| `Reference/osm_*.geojson` | Raw OpenStreetMap data |"""

    def _appendix_parameters(self) -> str:
        settings = self.settings
        dims = self.hm.get("dimensions", "unknown")
        parts = dims.split("x")
        vertex_x = int(parts[0]) if parts[0].isdigit() else 0
        face_x = vertex_x - 1 if vertex_x > 0 else 0

        return f"""## Appendix B: Terrain Parameters Reference

All values are pre-computed and ready to use:

```
Terrain Grid Size X:    {face_x}
Terrain Grid Size Z:    {face_x}
Grid Cell Size:         {self.hm.get('grid_cell_size_m', 2.0)}m
Terrain Size:           {self.hm.get('terrain_size_m', 'unknown')}
Height Scale:           {self.elev.get('height_scale', 0.03125):.6g}
Min Elevation:          {self.elev.get('min_elevation_m', 0):.1f}m
Max Elevation:          {self.elev.get('max_elevation_m', 0):.1f}m
Elevation Range:        {self.elev.get('max_elevation_m', 0) - self.elev.get('min_elevation_m', 0):.1f}m

Heightmap Dimensions:   {dims} pixels
Heightmap Format:       ESRI ASCII Grid (.asc) — recommended
                        16-bit PNG (.png) — alternative

Invert X Axis:          No
Invert Z Axis:          Yes

Default Surface:        {self.recommended_default} ({self.default_material})
Surface Masks:          {self.surf.get('count', 5)} masks
Block Violations:       {self.surf.get('block_saturation', {}).get('violations', 0)}

Coordinate System:      {self.input_data.get('crs', 'Unknown')}
Countries:              {', '.join(self.input_data.get('countries', ['Unknown']))}
```"""

    def _appendix_troubleshooting(self) -> str:
        return """## Appendix C: Troubleshooting

### Project won't open
- Ensure the folder is in the correct addons directory (NOT inside OneDrive)
- Verify ArmaReforger is listed as a dependency in your Projects panel
- Try removing and re-adding the project

### Terrain is flat after heightmap import
- Did you **Save** and reopen the world after import? This is required.
- Check that you imported `heightmap.asc` (not the preview PNG)
- Verify the **Height scale** value in the New Terrain dialog was entered correctly

### Surface masks look wrong
- Did you import in the correct order? (rock -> forest -> asphalt -> sand/dirt)
- Did you **Save** after importing all masks?
- Check the **Info & Diags** tab for block saturation issues
- Try re-importing: right-click surface > Priority Surface Mask Import

### Blocky artifacts on terrain
- Check block saturation: Info & Diags > Ctrl+X in viewport
- If blocks show Red in the 3x3 grid, use **Merge** to combine surfaces
- Consider reducing to 3-4 surface types in dense areas

### Roads not visible
- Ensure the **roads** layer is enabled (checked) in the hierarchy
- Select a road entity and check it has a valid RoadGeneratorEntity child
- Try: right-click road > Generate Road

### No sky/atmosphere
- Check the **default** layer has GenericWorldEntity with sky presets
- Verify Lighting_Default.et is present and enabled

### Performance issues
- Large terrains (4096+ faces) may be slow in the editor
- Reduce World Editor quality settings
- Close unnecessary panels
- Consider working with a smaller terrain first"""

    def _appendix_next_steps(self) -> str:
        return """## Appendix D: Next Steps

Once your basic terrain is set up, consider:

1. **Vegetation density**: Use Forest Generator to add tree cover matching your surface masks
2. **Building placement**: Reference `features.json` for building locations and types
3. **NavMesh generation**: Required for AI pathfinding — generate via World Editor tools
4. **Additional detail**: Add power lines, fences, rocks, and other environmental objects
5. **Lighting refinement**: Adjust sun angle, fog density, and time of day
6. **Ocean setup**: If your terrain has coastline, configure ocean properties on the WorldEntity
7. **Publishing**: When ready, use Workbench > Publish Addon to share your map

---

*Generated by [Arma Reforger Base Map Generator](https://github.com/tubalainen/arma_reforger_base_map_generator_ng)*"""
