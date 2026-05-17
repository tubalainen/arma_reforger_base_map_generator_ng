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
    APP_VERSION,
    SURFACE_MATERIAL_MAP,
    SURFACE_MATERIAL_ALTERNATIVES,
    SURFACE_IMPORT_ORDER,
    DEFAULT_ADDON_DIR,
    MANDATORY_BOOTSTRAP_KEYS,
    WORLD_PREFABS,
    resolve_ambient_prefab,
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
        self.feature_sources = metadata.get("feature_sources", {})

        # Coverage data
        self.coverage = self.surf.get("coverage", {})
        self.coverage_per_surface = self.coverage.get("per_surface", {})
        self.recommended_default = self.coverage.get("recommended_default", "grass")
        self.default_material = self.coverage.get(
            "recommended_default_material",
            SURFACE_MATERIAL_MAP.get("grass", "Grass_01.emat"),
        )

        # Only surfaces actually generated (non-empty masks)
        # Falls back to full SURFACE_IMPORT_ORDER for backwards compatibility
        self.surfaces_present = set(
            self.surf.get("surfaces_present", ["grass"] + list(SURFACE_IMPORT_ORDER))
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
            self._phase_bootstrap_entities(),
            self._phase_surface_painting(),
            self._phase_satellite_map(),
            self._phase_roads(),
            self._phase_vegetation_water(),
            self._phase_testing(),
            self._known_limitations(),
            self._appendix_files(),
            self._appendix_parameters(),
            self._appendix_troubleshooting(),
            self._appendix_data_sources(),
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
        country_codes = self.input_data.get("countries", []) or []
        countries = ", ".join(country_codes) or "Unknown"
        crs = self.input_data.get("crs", "Unknown")

        block_violations = self.surf.get("block_saturation", {}).get("violations", 0)
        total_blocks = self.surf.get("block_saturation", {}).get("total_blocks", 0)
        ambient_prefab = resolve_ambient_prefab(country_codes).split("/")[-1]

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
| **Bootstrap Entities** | {len(MANDATORY_BOOTSTRAP_KEYS)} + 1 ambient auto-wired |
| **Ambient Sound** | {ambient_prefab} |
| **Estimated Setup Time** | ~20-30 minutes |"""

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

    def _phase_bootstrap_entities(self) -> str:
        """
        Phase 3: Bootstrap entities — explains what the generator pre-wired
        in the managers layer and what (if anything) the user still has to
        add manually. v1.4.0 — Atlas 2 alignment, addresses issue #81.
        """
        countries = self.input_data.get("countries", []) or []
        ambient = resolve_ambient_prefab(countries)
        rows = []
        for key in MANDATORY_BOOTSTRAP_KEYS:
            path = WORLD_PREFABS.get(key, "(unknown)")
            short = path.split("/")[-1]
            rows.append(f"| `{key}` | `{short}` | auto-wired in managers layer |")
        rows.append(
            f"| `ambient_sounds` | `{ambient.split('/')[-1]}` | "
            f"auto-wired (biome-matched for {', '.join(countries) or 'default'}) |"
        )
        table = "\n".join(rows)

        return f"""## Phase 3: Bootstrap Entities (v1.4.0 — Atlas 2 alignment)

These entities make the world load to a fully functional Game-Master-ready
state. The generator now writes them into `Worlds/{self.map_name}_Layers/managers.layer`
automatically, so a freshly-imported world plays without any manual entity
drags — resolving issue #81. The list below is for verification only.

| Entity key | Prefab | Status |
|------------|--------|--------|
{table}

> **What if one is missing in Workbench?** A handful of paths may differ
> between Reforger versions. If Workbench logs "resource not registered"
> for a specific entry, open the Resource Browser, search for the short
> name in the table above, and drag the result into the managers layer.
> Then update `webapp/config/enfusion.py::WORLD_PREFABS` so subsequent
> generations use the correct path.

> **Critical**: never rename the `default` layer. Several Reforger
> subsystems hard-code its name (this is the only naming rule Atlas 2
> explicitly calls out)."""

    def _phase_surface_painting(self) -> str:
        lines = [f"""## Phase 4: Surface Painting (15 minutes)

> **Atlas 2 rule:** import **dirt-type surfaces first**, then **grass-type**
> surfaces. The parallax map composites in the order the masks are applied,
> so reversing this washes out the rougher textures. The
> `surface_assignments.json` file at the project root lists the exact
> import order (`surface_import_order` array).

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

        # Step 3.3: Add surface materials. The user should only see entries
        # for surfaces that were actually generated AND have non-trivial
        # coverage on the terrain — walking the user through a "rock" import
        # for a flat coastal map (0.0% rock) wastes time and breeds distrust
        # of the guide.
        def _has_meaningful_coverage(surface_name: str) -> bool:
            entry = self.coverage_per_surface.get(surface_name, {})
            try:
                pct = float(entry.get("percentage", 0.0))
            except (TypeError, ValueError):
                pct = 0.0
            return pct > 0.0

        present_ordered = [
            s
            for s in SURFACE_IMPORT_ORDER
            if s in self.surfaces_present and _has_meaningful_coverage(s)
        ]

        lines.append(f"""### Step 3.3: Add Surface Materials

{len(present_ordered)} surface mask(s) were generated for this area. Add the corresponding materials to the Paint panel:""")

        for i, surface_name in enumerate(present_ordered, 1):
            material = SURFACE_MATERIAL_MAP.get(surface_name, "Unknown.emat")
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

        verification_text = {
            "rock": "Mountain peaks and steep slopes should now show rock texture",
            "pine_floor": "Coniferous forest areas should show pine needle/bark texture",
            "forest_floor": "Deciduous forest areas should show dark earth/leaf litter texture",
            "asphalt": "Roads and urban areas should show paved surface",
            "gravel": "Gravel roads and tracks should show gravel texture",
            "dirt": "Farmland and dirt paths should show bare earth texture",
            "sand": "Beaches, shorelines, and underwater seabed should show sand texture",
            "water_edge": "Near-water transition zones should show wet/muddy texture",
        }

        for i, surface_name in enumerate(present_ordered, 1):
            material = SURFACE_MATERIAL_MAP.get(surface_name, "Unknown.emat")
            material_short = material.split("/")[-1].replace(".emat", "")
            pct = self.coverage_per_surface.get(surface_name, {}).get("percentage", "?")
            verification = verification_text.get(surface_name, "Surface should be visible in the expected areas")

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
            return """## Phase 5: Satellite Map (Skipped)

No satellite imagery was available for this region. You can add satellite imagery
manually later via Terrain Tool (Ctrl+T) > Manage tab > Import Satellite Map."""

        return f"""## Phase 5: Satellite Map (5 minutes)

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

> **Source**: {self.satellite.get('source', 'Sentinel-2 Cloudless')} imagery ({self.satellite.get('dimensions', 'unknown')} pixels)"""

    def _phase_roads(self) -> str:
        road_count = self.roads.get("total_segments", 0)

        if road_count == 0:
            return """## Phase 6: Roads (Skipped)

No roads were found in the selected area."""

        by_surface = self.roads.get("by_surface", {})
        surface_str = ", ".join(f"{k}: {v}" for k, v in by_surface.items())

        return f"""## Phase 6: Roads (manual generator attach)

Your terrain has **{road_count}** road segments ({surface_str}).

The roads layer (`Worlds/{self.map_name}_Layers/roads.layer`) carries one
**SplineShapeEntity** per road segment, projected to Enfusion local
coordinates and following terrain elevation.

**v1.4.0 — Atlas 2 alignment:** splines are no longer named
`Road_0..N`. The generator now derives a descriptive name from OSM tags
(falling back to surface + quadrant for anonymous ways) so the hierarchy
panel tells you what each road is at a glance:

```
SplineShapeEntity Road_E4_Asphalt   {{ // E4 | prefab: RG_Road_Asphalt_E_03 | paints: asphalt | fq: {{8B67F44381CD2216}}PrefabLibrary/Generators/Roads/Asphalt/RG_Road_Asphalt_E_03.et
SplineShapeEntity Road_Storgatan_Asphalt {{ // Storgatan | prefab: RG_Road_Asphalt_E_01_DashedLine | paints: asphalt | fq: {{5E336AEB0923963F}}PrefabLibrary/Generators/Roads/Asphalt/RG_Road_Asphalt_E_01_DashedLine.et
SplineShapeEntity Road_Asphalt_NE_001 {{ // prefab: RG_Road_Asphalt_E_01_Narrow | paints: asphalt | fq: {{31086BE1AF790FC5}}PrefabLibrary/Generators/Roads/Asphalt/RG_Road_Asphalt_E_01_Narrow.et
```

The `fq:` token in each comment is the fully-qualified `{{guid}}path.et`
string you can paste directly into the **RoadGeneratorEntity > Prefab**
field, taking the GUID from Atlas 2's `SCR_SHPPrefabDataList` (the
canonical source — see [`docs/Atlas2.pdf`](../docs/Atlas2.pdf) p. 12).

Prefab names are the **Atlas 2 canonical set** (`RG_Road_Asphalt_E_01..03`,
`RG_Road_Asphalt_E_01_DashedLine`, `RG_Road_Asphalt_E_01_Narrow`,
`RG_Road_Dirt_01`, `RG_Road_Dirt_02`, `RG_Road_Forest_01`,
`RG_Road_Cobblestone_01`, `RG_TrailDirt_01`, `RG_TrailGravel_01`) —
all under `PrefabLibrary/Generators/Roads/{{Asphalt|Cobblestone|Dirt}}/`.

Splines are emitted **without** an attached `RoadGeneratorEntity` child —
v1.1.0 attempted to auto-attach the generator but the resulting nested
prefab syntax hangs the World Editor at 4% on world load. v1.2.3 reverts
that behaviour. Attach the generator manually as described below.

### Step 5.1: Verify Splines Loaded

1. In the World Editor hierarchy, expand the **roads** layer
2. Make sure the layer's visibility checkbox is enabled (eye icon)
3. You should see one **SplineShapeEntity** per road segment, with a `//`
   comment listing the road name and the suggested prefab name
4. Splines include elevation data so they follow the terrain surface

### Step 5.2: Attach a `RoadGeneratorEntity` to Each Spline

1. Select the **SplineShapeEntity** in the hierarchy
2. Right-click > **Add Child Entity** > **RoadGeneratorEntity**
3. In the new child's properties, set the **Prefab** field to the value
   shown in the spline's `// prefab: …` comment, fully qualified to
   `Prefabs/WEGenerators/Roads/<prefab>.et`
4. Optionally enable **Adjust Height Map** on the generator to carve the
   road into the terrain
5. Repeat for each road spline. `Reference/roads_reference.csv` has the
   complete per-road prefab list if you want to script this in bulk.

### Step 5.3: Reference Data

- `Reference/roads_reference.csv` — road index, type, surface, width, and
  the suggested known-good prefab
- `Reference/roads_enfusion.geojson` — road data with local coordinates
- `Reference/roads_splines.csv` — spline control points in local metres

> **Note**: Road prefabs are located at `Prefabs/WEGenerators/Roads/` in
> the ArmaReforger data. The suggested prefab is snapped to a known-good
> name (asphalt, gravel, dirt at the widths shipped with stock Reforger),
> so it should load without errors."""

    def _phase_vegetation_water(self) -> str:
        lakes = self.features.get("lakes", 0)
        rivers = self.features.get("rivers", 0)
        forests = self.features.get("forest_areas", 0)

        return f"""## Phase 7: Vegetation & Water (Generators)

> **v1.4.0 — Atlas 2 alignment:** vegetation and water splines now use
> descriptive names derived from OSM tags. Forests are
> `Forest_<species>_<quadrant>_<NNN>` (`Forest_Pine_NE_001`,
> `Forest_Deciduous_SW_004`). Lakes use the OSM name when present
> (`Lake_Vanern`, `Lake_Storsjon`) and fall back to `Lake_<quadrant>_<NNN>`.
> Rivers use the OSM name (`River_Dalalven`) or `River_<quadrant>_<NNN>`
> for anonymous waterways.
>
> Reference prefabs Atlas 2 calls out by name
> ([`docs/Atlas2.pdf`](../docs/Atlas2.pdf)):
>
> - Rivers: drag `R_RiverMedium_01.et` onto the river spline (Resource
>   Browser → search "RiverMedium" or navigate
>   `ArmaReforger > Prefabs > World > Water > River`).
> - Forests: `FG_Forest_Spruce1.et`, `FG_Forest_Pine1.et` (note the
>   trailing `1`, no underscore separator).

The vegetation and water layers contain pre-drawn splines projected to
Enfusion local coordinates and clipped to the terrain. When a Forest or
Lake Generator prefab path is configured in the catalog (see below), the
generator child is auto-attached and the area fills with trees or water on
world load — no manual prefab-dropping needed.

By default the catalogs ship empty (the same safe pattern used for buildings),
so manual wiring is the fallback until you confirm the prefab paths match your
stock Reforger install.

### Step 6.1: Forest Generator

Your terrain has **{forests}** forest areas identified from OpenStreetMap.
The vegetation layer contains a closed `SplineShapeEntity` for each.

**If Forest Generator prefabs are auto-attached** (catalog populated):
- The generator child already exists inside each spline — open the project
  in Workbench, select any `ForestArea_*` spline, and you will see the
  `FG_*.et` child in the entity tree. No further wiring needed.
- Enable **Avoid Roads** and **Avoid Lakes** on each generator if not set.

**If the catalog is empty** (default — manual wiring):
1. Select the spline in the World Editor (vegetation layer)
2. Drag a Forest Generator prefab from `Prefabs/WEGenerators/Forest/`
   (prefixed `FG_`) onto the spline — it will populate the area with trees
3. Enable **Avoid Roads** and **Avoid Lakes** on the generator

> **To enable auto-attachment**: edit
> `webapp/config/forests.py::KNOWN_FOREST_PREFABS` and add entries mapping
> forest type keys (`"coniferous"`, `"deciduous"`, `"mixed"`) to the actual
> `FG_*.et` paths from your Reforger install. No code changes — just config
> edits. The leaf_type metadata in `Reference/osm_forests.geojson` shows
> which type applies to each polygon.

### Step 6.2: Water Bodies

Your terrain has **{lakes}** lakes/ponds/reservoirs and **{rivers}** rivers/streams.

**Lakes (closed splines)**: The water layer contains one closed `SplineShapeEntity`
per lake/pond/reservoir.

**If Lake Generator prefabs are auto-attached** (catalog populated):
- The `LG_*.et` child already exists inside each spline — no manual wiring needed.
- Enable **Flatten By Bottom Plane** on each generator if not set.

**If the catalog is empty** (default — manual wiring):
1. Select the lake spline in the World Editor (water layer)
2. Drag a Lake Generator prefab from `Prefabs/WEGenerators/Water/Lake/`
   (prefixed `LG_`) onto the spline
3. Enable **Flatten By Bottom Plane** for natural water level

> **To enable auto-attachment**: edit
> `webapp/config/lakes.py::KNOWN_LAKE_PREFABS` and add entries mapping
> water type keys (`"lake"`, `"pond"`, `"reservoir"`) to confirmed `LG_*.et`
> paths. No code changes needed.

**Rivers (open splines)**: The water layer also contains one open
`SplineShapeEntity` per river/stream/canal LineString, labelled with the
OSM name and estimated width. These are pre-positioned markers — add a
river generator child manually if your project uses one.

### Step 6.3: Buildings

Your terrain has **{self.features.get('buildings', 0)}** building footprints
extracted from OpenStreetMap. As of v1.4.1, the buildings layer
(`Worlds/{self.map_name}_Layers/buildings.layer`) auto-places every building as a
positioned `Prefabs/Structures/.../Building_*.et` instance — no manual
prefab-dropping needed.

OSM `building=<type>` tags are mapped to verified stock Reforger prefabs:

| OSM building type        | Reforger prefab |
|--------------------------|-----------------|
| `house` / `detached`     | `House_Village_E_1I01` (single-storey village house) |
| `residential`            | `House_Town_E_2I01` (two-storey town house) |
| `apartments`             | `Villa_E_2I01` |
| `church` / `chapel`      | `Church_01` |
| `commercial` / `retail` / `school` / `hospital` / `office` | `ShopModern_E_01` |
| `industrial` / `warehouse` | `Office_E_01` |
| `garage` / `garages`     | `HouseAddon_Garage_E_01` |
| `barn` / `farm`          | `Barn_E_03_closed` |
| `farm_auxiliary` / `shed` | `Shed_01` |
| `yes` (no detail)        | `House_Village_E_1I01` (fallback) |

Every path was harvested from public Reforger mod source on GitHub and
verified to load successfully in stock Workbench — see the docstring at
[`webapp/config/buildings.py`](../webapp/config/buildings.py) for the
sourcing methodology.

Buildings whose centroid would have fallen on top of an asphalt road have
been dropped automatically (so traffic/pathing isn't broken — known as the
"L12 de-conflict" step).

**To swap a variant** (e.g. use a wooden house instead of `House_Village_E_1I01`
for `Building_House`): edit
`webapp/config/buildings.py::KNOWN_BUILDING_PREFABS` and change the path.
The next generation picks up the change automatically. **To go back to
footprint-marker mode** for a category: remove its entry from the catalogue.

> Source data: `Reference/osm_buildings.geojson` and `features.json`
> (look for the `buildings` array)."""

    def _phase_testing(self) -> str:
        return f"""## Phase 8: Testing (5 minutes)

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

    def _surface_mask_file_table(self) -> str:
        """Return markdown table rows for surface masks present in this generation."""
        descriptions = {
            "forest_floor": "Surface mask: deciduous forest floor",
            "pine_floor": "Surface mask: coniferous forest floor",
            "asphalt": "Surface mask: paved areas",
            "gravel": "Surface mask: gravel roads",
            "dirt": "Surface mask: farmland/dirt",
            "rock": "Surface mask: rock/slopes",
            "sand": "Surface mask: sand/seabed",
            "water_edge": "Surface mask: water edge/mud",
        }
        rows = []
        for name in SURFACE_IMPORT_ORDER:
            if name in self.surfaces_present:
                desc = descriptions.get(name, f"Surface mask: {name.replace('_', ' ')}")
                rows.append(f"| `Sourcefiles/surface_{name}.png` | {desc} |")
        return "\n".join(rows) + ("\n" if rows else "")

    def _appendix_files(self) -> str:
        return f"""## Appendix A: File Reference

### Project Files
| File | Purpose |
|------|---------|
| `addon.gproj` | Enfusion project definition |
| `Worlds/{self.map_name}.ent` | World file (empty — editor populates BSP + bounds on save) |
| `Worlds/{self.map_name}_Layers/default.layer` | Terrain entity + atmosphere prefabs |
| `Worlds/{self.map_name}_Layers/managers.layer` | Camera, weather, audio, destruction managers |
| `Worlds/{self.map_name}_Layers/gamemode.layer` | Game Master mode (empty stub in v1.5.0) |
| `Worlds/{self.map_name}_Layers/roads.layer` | Pre-generated road entities |
| `Worlds/{self.map_name}_Layers/vegetation.layer` | Placeholder for forest generators |
| `Worlds/{self.map_name}_Layers/water.layer` | Placeholder for water entities |
| `Worlds/{self.map_name}_Layers/buildings.layer` | OSM-detected building entities |
| `Missions/{self.map_name}.conf` | Mission header (makes world playable) |

### Source Files (for import into Workbench)
| File | Purpose |
|------|---------|
| `Sourcefiles/heightmap.asc` | Primary heightmap (ESRI ASCII Grid) — **use this** |
| `Sourcefiles/heightmap.png` | Alternative heightmap (16-bit PNG) |
| `Sourcefiles/heightmap_preview.png` | Visual preview of elevation |
| `Sourcefiles/satellite_map.png` | {self.satellite.get('source', 'Sentinel-2')} satellite imagery |
| `Sourcefiles/surface_grass.png` | Surface mask: grass/meadow (always present) |
{self._surface_mask_file_table()}| `Sourcefiles/surface_preview.png` | Combined surface preview |

### Reference Files (for manual placement)
| File | Purpose |
|------|---------|
| `Reference/roads_enfusion.geojson` | Road data with local coordinates |
| `Reference/roads_splines.csv` | Road spline points (local metres) |
| `Reference/roads_reference.csv` | Road type/surface/width for manual prefab setup |
| `Reference/features.json` | Lakes, rivers, forests, buildings |
| `Reference/metadata.json` | Full generation metadata |
| `Reference/osm_*.geojson` | Raw OpenStreetMap data |
| `surface_assignments.json` | Spline → surface mask mapping + Atlas 2 import order (v1.4.0) |"""

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

### Workbench crashes the instant you brush any surface paint (issue #111)
Look in `error.log` for lines like:
```
RESOURCES (E): Wrong GUID/name for resource @"{58D0FB3206B6F859}Prefabs/..."
WORLD     (E): Unknown keyword/data 'TerrainGridSizeX' at offset ...
```
If those appear, the map was generated by a pre-1.5 build (≤ v1.4.10). The
generator was emitting every world prefab with the addon-level GUID instead
of the per-prefab GUID Workbench's resource DB requires, so the terrain
entity loaded broken and NVTT crashed on the first brush stroke
(`nvtt::CubeSurface::toGamma` access violation). **Regenerate the map with
v1.5.0 or newer** — the fix re-wires every world prefab with its real
verified GUID and drops the inline terrain-grid properties (those belong in
`tileMap.conf`, not the layer file). No manual workaround in Workbench.

### World hangs at a low percentage when opening
This is caused by SplineShapeEntities (forest or water areas) with too many
vertices for the Workbench renderer — raw OSM polygon boundaries can exceed
1,500 vertices per entity. The generator now caps splines at 200 points using
Ramer-Douglas-Peucker simplification, so this should not occur for newly
generated maps. If you have an older generated map, regenerate it from the
webapp to get simplified layers.

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
- Ensure the **roads** layer is **enabled** (eye icon checked) in the hierarchy
- Road splines are emitted spline-only — they need a `RoadGeneratorEntity`
  child to render. Right-click each SplineShapeEntity > **Add Child Entity**
  > **RoadGeneratorEntity** and set the prefab from the spline's `// prefab: …`
  comment (or `Reference/roads_reference.csv` for the full per-road list)
- Splines without a generator render as a thin debug line only

### "resource not registered" warnings for Reference/ files
Workbench may print warnings such as:
```
resource not registered: Reference/features.json
resource not registered: Reference/metadata.json
```
These are **expected and harmless**. The `Reference/` folder contains helper
files for you (building locations, road list, generation metadata) — they are
not Enfusion resources. Workbench scans the entire addon directory and warns
about any file it does not recognise. You can safely ignore these warnings.

### No sky/atmosphere
- v1.5.0 no longer hand-authors the `GenericWorldEntity { SkyPreset … }`
  block — Workbench rebuilds it on first **Save World**, after which the
  sky should appear. Save once, close the world, reopen.
- Verify `Lighting_Default.et` is present in the default layer and enabled.
- Open the Environment tab on `GenericWorldEntity world` and confirm the
  sky / planets / clouds / ocean preset slots are populated.

### Performance issues
- Large terrains (4096+ faces) may be slow in the editor
- Reduce World Editor quality settings
- Close unnecessary panels
- Consider working with a smaller terrain first"""

    def _appendix_data_sources(self) -> str:
        """Appendix D — concrete record of which APIs/datasets supplied
        this specific generation (issue #75). Pulls live source strings out
        of metadata for elevation, satellite, and per-feature-category vector
        providers, then renders fixed attribution lines."""
        elev_source = self.elev.get("source", "Unknown")
        elev_res = self.elev.get("resolution_m")
        elev_line = f"- **Elevation:** {elev_source}"
        if elev_res is not None:
            elev_line += f" ({elev_res} m resolution)"

        sat_lines = []
        if self.satellite:
            sat_source = self.satellite.get("source", "Sentinel-2 Cloudless (EOX)")
            sat_dims = self.satellite.get("dimensions", "")
            sat_line = f"- **Satellite imagery:** {sat_source}"
            if sat_dims:
                sat_line += f" ({sat_dims})"
            sat_lines.append(sat_line)
        else:
            sat_lines.append("- **Satellite imagery:** not generated")

        countries = self.input_data.get("countries") or []
        if not countries:
            countries = [self.input_data.get("primary_country", "UNKNOWN")]

        feature_rows = []
        if self.feature_sources:
            for category in ("roads", "buildings", "water", "forests", "land_use"):
                src = self.feature_sources.get(category)
                if src:
                    feature_rows.append(f"| {category.replace('_', ' ').title():<10} | {src} |")
        if not feature_rows:
            feature_rows.append("| (no per-category source info recorded) | |")
        feature_table = "\n".join(feature_rows)

        attribution_lines = []
        sources_seen = set()
        sources_seen.add(elev_source)
        if self.satellite:
            sources_seen.add(self.satellite.get("source", ""))
        for v in self.feature_sources.values():
            sources_seen.add(v)

        def _matches(needle: str) -> bool:
            return any(needle.lower() in s.lower() for s in sources_seen if s)

        if _matches("OpenStreetMap"):
            attribution_lines.append(
                "- **OpenStreetMap:** © OpenStreetMap contributors, "
                "licensed under the [Open Database License (ODbL)]"
                "(https://www.openstreetmap.org/copyright)."
            )
        if _matches("Lantmäteriet") or _matches("Hydrografi") or _matches("Marktäcke") or _matches("STAC"):
            attribution_lines.append(
                "- **Lantmäteriet** (STAC Höjd / STAC Bild / Hydrografi / Marktäcke): "
                "open data under [CC0 1.0]"
                "(https://creativecommons.org/publicdomain/zero/1.0/) "
                "via the Lantmäteriet open data programme."
            )
        if _matches("Sentinel-2") or _matches("EOX"):
            attribution_lines.append(
                "- **Sentinel-2 Cloudless** (EOX): Contains modified Copernicus "
                "Sentinel data 2024, processed by EOX IT Services GmbH."
            )
        if _matches("Copernicus DEM") or _matches("COP30") or _matches("AWS"):
            attribution_lines.append(
                "- **Copernicus DEM GLO-30:** © DLR e.V. 2010-2014 and "
                "© Airbus Defence and Space GmbH 2014-2018, provided under "
                "the [Copernicus Data License]"
                "(https://spacedata.copernicus.eu/web/cscda/data-access/cop-dem)."
            )
        if _matches("OpenTopography"):
            attribution_lines.append(
                "- **OpenTopography:** SRTM / COP30 access via "
                "[OpenTopography.org](https://opentopography.org/)."
            )
        if _matches("ALOS"):
            attribution_lines.append(
                "- **ALOS World 3D 30 m (AW3D30):** © JAXA, distributed via the "
                "[JAXA Earth Observation Research Center]"
                "(https://www.eorc.jaxa.jp/ALOS/en/dataset/aw3d30/aw3d30_e.htm)."
            )
        if _matches("Kartverket"):
            attribution_lines.append(
                "- **Kartverket** (Norway): open data under "
                "[NLOD 2.0](https://data.norge.no/nlod/en/2.0/)."
            )
        if _matches("Maa-amet"):
            attribution_lines.append(
                "- **Maa-amet** (Estonia): open data, "
                "[Maa-amet open data terms](https://geoportaal.maaamet.ee/eng/)."
            )
        if _matches("NLS"):
            attribution_lines.append(
                "- **NLS Finland:** open data under the "
                "[NLS open data licence]"
                "(https://www.maanmittauslaitos.fi/en/opendata-licence-version1)."
            )
        if _matches("Dataforsyningen"):
            attribution_lines.append(
                "- **Dataforsyningen** (Denmark): open data via "
                "[Dataforsyningen](https://dataforsyningen.dk/)."
            )
        if _matches("GUGiK"):
            attribution_lines.append(
                "- **GUGiK** (Poland): open data via "
                "[GUGiK Geoportal](https://www.geoportal.gov.pl/)."
            )
        # Country detection is always Natural Earth.
        attribution_lines.append(
            "- **Country detection:** [Natural Earth 10m Admin 0 Countries]"
            "(https://www.naturalearthdata.com/) — public domain."
        )

        attribution_block = "\n".join(attribution_lines)
        country_str = ", ".join(countries)

        return f"""## Appendix D: Data Sources

This map was generated using the following sources. Source selection is
automatic per area — the table below records exactly what was used for
**this** generation.

### Per-layer providers

{elev_line}
{chr(10).join(sat_lines)}
- **Country detection:** Natural Earth 10m Admin 0 Countries (public domain)
- **Covered countries:** {country_str}

| Feature    | Provider |
|------------|----------|
{feature_table}

### Attribution & licences

{attribution_block}

If you republish or redistribute the generated map (e.g. as a Workshop
upload), please preserve the attribution above so each upstream dataset
is properly credited."""

    def _appendix_next_steps(self) -> str:
        return f"""## Appendix E: Next Steps

Once your basic terrain is set up, consider:

1. **Vegetation density**: Use Forest Generator to add tree cover matching your surface masks
2. **Building placement**: Reference `features.json` for building locations and types
3. **NavMesh generation**: Required for AI pathfinding — generate via World Editor tools
4. **Additional detail**: Add power lines, fences, rocks, and other environmental objects
5. **Lighting refinement**: Adjust sun angle, fog density, and time of day
6. **Ocean setup**: If your terrain has coastline, configure ocean properties on the WorldEntity
7. **Publishing**: When ready, use Workbench > Publish Addon to share your map

---

*Generated by [Arma Reforger Base Map Generator](https://github.com/tubalainen/arma_reforger_base_map_generator_ng) **v{APP_VERSION}***"""
