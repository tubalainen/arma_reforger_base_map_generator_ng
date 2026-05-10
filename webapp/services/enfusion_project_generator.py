"""
Enfusion Workbench project file generator.

Generates all files required for a complete, ready-to-open Enfusion mod project:
- addon.gproj (project definition)
- World .ent file (layer index)
- Layer files (default, managers, gamemode, roads, vegetation, water)
- Mission .conf file (mission header)
- .meta files (resource metadata)

File format: Enfusion text serialization (C-like syntax).
All values are computed from the map generation metadata.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

import math

from config.enfusion import (
    ARMA_REFORGER_GUID,
    PLATFORM_CONFIGS,
    ROAD_PREFAB_BASE,
    FOREST_PREFAB_BASE,
    LAKE_PREFAB_BASE,
    WORLD_ENTITY_DEFAULTS,
    TERRAIN_LOD_DEFAULTS,
    WORLD_PREFABS,
    PROJECT_NAME_ALLOWED_CHARS,
    PROJECT_NAME_MAX_LENGTH,
    compute_height_scale,
)
from config.roads import validate_road_prefab
from config.forests import validate_forest_prefab, forest_type_from_osm
from config.lakes import validate_lake_prefab

logger = logging.getLogger(__name__)

# Estimated river widths by OSM water_type (same values as feature_extractor)
_RIVER_WIDTH_M = {
    "river": 15.0,
    "stream": 3.0,
    "canal": 8.0,
    "ditch": 1.5,
    "drain": 2.0,
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitize_project_name(name: str) -> str:
    """
    Convert a user-friendly name to a valid Enfusion project ID.

    Rules (from BI wiki "Mod Project Setup"):
    - Letters, numbers, spaces, hyphens, underscores, periods only
    - Max 64 characters

    Args:
        name: Raw user input name.

    Returns:
        Sanitized project name.
    """
    # Remove characters not allowed
    sanitized = "".join(c for c in name if c in PROJECT_NAME_ALLOWED_CHARS)

    # Collapse multiple spaces/underscores
    sanitized = re.sub(r"[\s_]+", "_", sanitized).strip("_. ")

    # Enforce max length
    if len(sanitized) > PROJECT_NAME_MAX_LENGTH:
        sanitized = sanitized[:PROJECT_NAME_MAX_LENGTH].rstrip("_. ")

    # Fallback if empty
    if not sanitized:
        sanitized = "GeneratedMap"

    return sanitized


def generate_guid(seed: str) -> str:
    """
    Generate a deterministic 16-character hex GUID from a seed string.

    Enfusion GUIDs are 16 hex characters. We use a SHA-256 hash of the
    seed truncated to 16 chars for deterministic reproducibility.

    Args:
        seed: Seed string (e.g., map_name + timestamp).

    Returns:
        16-character uppercase hex string.
    """
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return h[:16].upper()


def _indent(text: str, level: int = 1) -> str:
    """Indent text by the given number of levels (1 space per level, Enfusion style)."""
    prefix = " " * level
    return "\n".join(prefix + line if line.strip() else line for line in text.splitlines())


# ---------------------------------------------------------------------------
# Generator class
# ---------------------------------------------------------------------------

class EnfusionProjectGenerator:
    """
    Generates all Enfusion Workbench project files from map generation metadata.

    Usage:
        gen = EnfusionProjectGenerator("MyMap", metadata, road_data)
        files = gen.generate_all(output_dir)
    """

    def __init__(
        self,
        map_name: str,
        metadata: dict,
        road_data: Optional[dict] = None,
        transformer=None,
        elevation_array=None,
        forest_features: Optional[dict] = None,
        water_features: Optional[dict] = None,
        building_data: Optional[dict] = None,
    ):
        """
        Initialize the project generator.

        Args:
            map_name: User-provided map name (will be sanitized).
            metadata: Full map generation metadata dict.
            road_data: Optional processed road data for road layer generation.
            transformer: Optional CoordinateTransformer for road coordinate conversion.
            elevation_array: Optional DEM array (metres) for road elevation sampling.
            forest_features: Optional GeoJSON FeatureCollection of forest polygons
                             (from osm_data["forests"]). When supplied with a transformer,
                             vegetation.layer is populated with one closed SplineShapeEntity
                             per forest polygon (drag a Forest Generator prefab onto each).
            water_features: Optional GeoJSON FeatureCollection of water polygons
                            (from osm_data["water"]). Same treatment for water.layer
                            (drag a Lake Generator prefab onto each spline).
            building_data: Optional dict from feature_extractor.extract_building_features
                           with a "buildings" list. When supplied, buildings.layer is
                           populated with one entity per building (positioned prefab
                           instance if a validated Enfusion prefab is in
                           config.buildings.KNOWN_BUILDING_PREFABS, otherwise a
                           closed-spline footprint marker the user wires manually).
                           Buildings whose centroid falls inside an asphalt-road
                           buffer are dropped (audit task L12).
        """
        self.map_name = sanitize_project_name(map_name)
        self.metadata = metadata
        self.road_data = road_data
        self.transformer = transformer
        self.elevation_array = elevation_array
        self.forest_features = forest_features
        self.water_features = water_features
        self.building_data = building_data

        # Generate a deterministic GUID from the map name
        self.project_guid = generate_guid(self.map_name)

        # Extract key values from metadata
        self._extract_terrain_params()

        logger.info(
            f"EnfusionProjectGenerator initialized: name={self.map_name}, "
            f"guid={self.project_guid}, faces={self.face_count_x}x{self.face_count_z}"
        )

    def _extract_terrain_params(self):
        """Extract terrain parameters from metadata."""
        hm = self.metadata.get("heightmap", {})
        elev = self.metadata.get("elevation", {})

        # Parse dimensions from "WxH" string
        dims_str = hm.get("dimensions", "2049x2049")
        parts = dims_str.split("x")
        self.vertex_count_x = int(parts[0])
        self.vertex_count_z = int(parts[1]) if len(parts) > 1 else self.vertex_count_x

        # Faces = vertices - 1
        self.face_count_x = self.vertex_count_x - 1
        self.face_count_z = self.vertex_count_z - 1

        # Cell size
        self.cell_size = float(hm.get("grid_cell_size_m", 2.0))

        # Terrain size in metres
        self.terrain_width = self.face_count_x * self.cell_size
        self.terrain_depth = self.face_count_z * self.cell_size

        # Elevation
        self.min_elevation = float(elev.get("min_elevation_m", 0))
        self.max_elevation = float(elev.get("max_elevation_m", 100))
        self.height_scale = float(elev.get("height_scale", 0.03125))
        self.height_offset = float(elev.get("height_offset", self.min_elevation))

        # Location (for weather manager)
        input_data = self.metadata.get("input", {})
        bbox = input_data.get("bbox", {})
        self.center_lat = (bbox.get("south", 0) + bbox.get("north", 0)) / 2
        self.center_lon = (bbox.get("west", 0) + bbox.get("east", 0)) / 2

    def generate_all(self, output_dir: Path, job=None) -> dict:
        """
        Generate all project files in the output directory.

        Creates the full Enfusion project structure:
          <MapName>/
            addon.gproj
            Worlds/
              <MapName>.ent
              <MapName>.ent.meta
              <MapName>_default.layer
              <MapName>_managers.layer
              <MapName>_gamemode.layer
              <MapName>_roads.layer
              <MapName>_vegetation.layer
              <MapName>_water.layer
            Missions/
              <MapName>.conf
              <MapName>.conf.meta

        Args:
            output_dir: Root output directory for the project.

        Returns:
            Dict mapping file keys to their generated file paths.
        """
        # Create directory structure
        worlds_dir = output_dir / "Worlds"
        missions_dir = output_dir / "Missions"
        worlds_dir.mkdir(parents=True, exist_ok=True)
        missions_dir.mkdir(parents=True, exist_ok=True)

        if job:
            job.add_log("Generating addon project file...")

        files = {}

        # Project definition
        files["addon.gproj"] = self._write_file(
            output_dir / "addon.gproj",
            self._generate_gproj()
        )

        # World file (layer index)
        files["world.ent"] = self._write_file(
            worlds_dir / f"{self.map_name}.ent",
            self._generate_world_ent()
        )

        # World metadata
        files["world.ent.meta"] = self._write_file(
            worlds_dir / f"{self.map_name}.ent.meta",
            self._generate_meta("ent", f"Worlds/{self.map_name}.ent")
        )

        # Layer files
        files["default.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_default.layer",
            self._generate_default_layer()
        )

        files["managers.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_managers.layer",
            self._generate_managers_layer()
        )

        files["gamemode.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_gamemode.layer",
            self._generate_gamemode_layer()
        )

        if job:
            road_count = len(self.road_data.get("roads", [])) if self.road_data else 0
            job.add_log(f"Generating world file with {road_count} road entities...")
        files["roads.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_roads.layer",
            self._generate_roads_layer()
        )

        files["vegetation.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_vegetation.layer",
            self._generate_vegetation_layer()
        )

        files["water.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_water.layer",
            self._generate_water_layer()
        )

        files["buildings.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_buildings.layer",
            self._generate_buildings_layer()
        )

        # Mission header
        files["mission.conf"] = self._write_file(
            missions_dir / f"{self.map_name}.conf",
            self._generate_mission_conf()
        )

        files["mission.conf.meta"] = self._write_file(
            missions_dir / f"{self.map_name}.conf.meta",
            self._generate_meta("conf", f"Missions/{self.map_name}.conf")
        )

        logger.info(f"Generated {len(files)} Enfusion project files in {output_dir}")
        if job:
            job.add_log(f"Generated {len(files)} Enfusion project files", "success")
        return files

    def _write_file(self, path: Path, content: str) -> str:
        """Write content to file and return the path as string."""
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        logger.debug(f"Wrote: {path}")
        return str(path)

    # -----------------------------------------------------------------------
    # File generators
    # -----------------------------------------------------------------------

    def _generate_gproj(self) -> str:
        """Generate addon.gproj project definition."""
        configs = "\n".join(
            f'  GameProjectConfig {platform} {{\n  }}'
            for platform in PLATFORM_CONFIGS
        )

        return f'''GameProject {{
 ID "{self.map_name}"
 GUID "{self.project_guid}"
 TITLE "{self.map_name} - Generated Terrain"
 Dependencies {{
  "{ARMA_REFORGER_GUID}"
 }}
 Configurations {{
{configs}
 }}
}}
'''

    def _generate_world_ent(self) -> str:
        """Generate world .ent file (layer index)."""
        return f'''Layer default {{
 Index 0
}}
Layer managers {{
 Index 1
}}
Layer gamemode {{
 Index 2
}}
Layer roads {{
 Index 3
}}
Layer vegetation {{
 Index 4
}}
Layer water {{
 Index 5
}}
Layer buildings {{
 Index 6
}}
'''

    def _generate_meta(self, resource_type: str, resource_path: str) -> str:
        """
        Generate a .meta file for a resource.

        Args:
            resource_type: "ent" or "conf"
            resource_path: Relative path to the resource file.
        """
        # Determine resource class name
        if resource_type == "ent":
            class_name = "ENTResourceClass"
        elif resource_type == "conf":
            class_name = "CONFResourceClass"
        else:
            class_name = "ResourceClass"

        platforms = "\n".join(
            f'  {class_name} {platform}{" : PC" if platform != "PC" else ""} {{\n  }}'
            for platform in PLATFORM_CONFIGS
            if platform not in ("PS4", "PS5")  # PS not in standard meta configs
        )

        return f'''MetaFileClass {{
 Name "{{{self.project_guid}}}{resource_path}"
 Configurations {{
{platforms}
 }}
}}
'''

    def _generate_default_layer(self) -> str:
        """
        Generate the default layer with terrain entity, world entity, and environment setup.

        This is the most complex generated file. It includes:
        1. GenericWorldEntity — sky, atmosphere, ocean settings
        2. GenericTerrainEntity — using GenericTerrain_Default.et prefab
        3. Lighting_Default.et — default sun light
        4. FogHaze_Default.et — default fog
        5. GenericWorldPP_Default.et — post-processing defaults
        6. EnvProbe_Default.et — environment probe
        """
        we = WORLD_ENTITY_DEFAULTS
        tl = TERRAIN_LOD_DEFAULTS

        # Terrain center coordinates for camera positioning
        center_x = self.terrain_width / 2
        center_z = self.terrain_depth / 2
        camera_y = self.max_elevation + 200

        content = f'''GenericWorldEntity {{
 coords 0 0 0
 {{
  SkyPreset {{
   SkyPresetName "{we['sky_preset']}"
  }}
  PlanetPresets {{
   PlanetPreset {{
    PlanetName "{we['planet_presets'][0]}"
   }}
   PlanetPreset {{
    PlanetName "{we['planet_presets'][1]}"
   }}
   PlanetPreset {{
    PlanetName "{we['planet_presets'][2]}"
   }}
  }}
  SkyVolCloudsRenderer {{
   CloudsPreset "{we['clouds_preset']}"
  }}
  OceanPreset {{
   OceanMaterial "{we['ocean_material']}"
   OceanSimulation "{we['ocean_simulation']}"
  }}
 }}
}}
GenericTerrainEntity : "{{58D0FB3206B6F859}}{WORLD_PREFABS['terrain']}" {{
 coords 0 0 0
 TerrainGridSizeX {self.face_count_x}
 TerrainGridSizeZ {self.face_count_z}
 GridCellSize {self.cell_size}
 HeightScale {self.height_scale:.8f}
 HeightOffset {self.height_offset:.2f}
 CloseDistanceMax {tl['close_distance_max']}
 CloseDistanceBlend {tl['close_distance_blend']}
 MiddleDistanceMax {tl['middle_distance_max']}
 MiddleDistanceBlend {tl['middle_distance_blend']}
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['lighting']} {{
 coords {center_x:.1f} {camera_y:.1f} {center_z:.1f}
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['fog']} {{
 coords 0 0 0
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['post_processing']} {{
 coords 0 0 0
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['env_probe']} {{
 coords {center_x:.1f} 50 {center_z:.1f}
}}
'''
        return content

    def _generate_managers_layer(self) -> str:
        """Generate the managers layer with camera, weather, audio, etc."""
        center_x = self.terrain_width / 2
        center_z = self.terrain_depth / 2
        camera_y = self.max_elevation + 200

        content = f'''${{58D0FB3206B6F859}}{WORLD_PREFABS['camera']} {{
 coords {center_x:.1f} {camera_y:.1f} {center_z:.1f}
 PlayFromCameraPosition 1
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['time_weather']} {{
 coords 0 0 0
 Latitude {self.center_lat:.4f}
 Longitude {self.center_lon:.4f}
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['projectile_sounds']} {{
 coords 0 0 0
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['map_entity']} {{
 coords 0 0 0
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['sound_world']} {{
 coords 0 0 0
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['forest_sync']} {{
 coords 0 0 0
}}
${{58D0FB3206B6F859}}{WORLD_PREFABS['destruction']} {{
 coords 0 0 0
}}
'''
        return content

    def _generate_gamemode_layer(self) -> str:
        """Generate the game mode layer with Game Master mode for instant playability."""
        center_x = self.terrain_width / 2
        center_z = self.terrain_depth / 2

        return f'''${{58D0FB3206B6F859}}{WORLD_PREFABS['gamemode_editor']} {{
 coords {center_x:.1f} 0 {center_z:.1f}
}}
'''

    def _generate_roads_layer(self) -> str:
        """
        Generate the roads layer with one SplineShapeEntity per road and an
        auto-attached RoadGeneratorEntity child carrying the inferred prefab.

        Each road's ``enfusion_prefab`` field is run through
        ``validate_road_prefab`` so the emitted path always points at a known
        prefab in a stock Reforger install. The child uses the standard
        ``${guid}path/to/prefab.et { coords ... }`` instance syntax — the
        same pattern the managers layer uses to instantiate world prefabs.

        Spline points include Y (elevation) values sampled from the heightmap
        so roads follow the terrain surface.

        Entity format:
          SplineShapeEntity Road_N {
           coords X Y Z
           Points {
            ShapePoint sp_0 { Position 0 0 0 }
            ShapePoint sp_1 { Position relX relY relZ }
            ...
           }
           ${guid}Prefabs/WEGenerators/Roads/RG_Road_<Surface>_<W>m.et {
            coords 0 0 0
           }
          }

        Road entities are clipped to terrain bounds — roads with origin
        outside the terrain or fewer than 2 points within bounds are skipped.
        """
        if not self.road_data or not self.transformer:
            return (
                '// Road layer — no road data available or coordinate transformer not configured.\n'
                '// Use Reference/roads_enfusion.geojson to manually place roads.\n'
                '// Use Reference/roads_reference.csv for road type/surface/width details.\n'
            )

        roads = self.road_data.get("roads", [])
        if not roads:
            return '// Road layer — no road segments found in the area.\n'

        entities = []
        skipped = 0
        clipped = 0

        for i, road in enumerate(roads):
            points = road.get("spline_points", [])
            if len(points) < 2:
                skipped += 1
                continue

            # Transform points to local coordinates with elevation sampling
            local_points = self.transformer.transform_points(
                points,
                elevation_array=self.elevation_array,
            )

            # Clip: keep only points within terrain bounds (with small margin)
            margin = 1.0  # 1m margin to avoid edge issues
            in_bounds = [
                pt for pt in local_points
                if -margin <= pt["x"] <= self.terrain_width + margin
                and -margin <= pt["z"] <= self.terrain_depth + margin
            ]

            if len(in_bounds) < 2:
                clipped += 1
                continue

            # Use clipped points
            local_points = in_bounds

            # First point is the entity origin
            origin = local_points[0]

            # Build ShapePoint definitions (relative to entity origin)
            point_defs = []
            for j, pt in enumerate(local_points):
                rel_x = pt["x"] - origin["x"]
                rel_y = pt["y"] - origin["y"]
                rel_z = pt["z"] - origin["z"]
                point_defs.append(
                    f'   ShapePoint sp_{j} {{\n'
                    f'    Position {rel_x:.3f} {rel_y:.3f} {rel_z:.3f}\n'
                    f'   }}'
                )

            road_name = road.get("name", "").replace('"', "'")
            comment = f' // {road_name}' if road_name else ""

            prefab_name = validate_road_prefab(
                road.get("enfusion_prefab", "RG_Road_Asphalt_4m")
            )
            prefab_ref = (
                f'${{{ARMA_REFORGER_GUID}}}{ROAD_PREFAB_BASE}/{prefab_name}.et'
            )

            entity = (
                f'SplineShapeEntity Road_{i} {{{comment}\n'
                f' coords {origin["x"]:.3f} {origin["y"]:.3f} {origin["z"]:.3f}\n'
                f' Points {{\n'
                + "\n".join(point_defs) + "\n"
                f' }}\n'
                f' {prefab_ref} {{\n'
                f'  coords 0 0 0\n'
                f' }}\n'
                f'}}'
            )
            entities.append(entity)

        if skipped > 0:
            logger.info(f"Skipped {skipped} road segments with < 2 points")
        if clipped > 0:
            logger.info(f"Clipped {clipped} road segments outside terrain bounds")

        logger.info(
            f"Generated {len(entities)} road entities (each with auto-attached "
            f"RoadGeneratorEntity child) for roads layer"
        )
        return "\n".join(entities) + "\n"

    def _generate_vegetation_layer(self) -> str:
        """
        Emit one closed SplineShapeEntity per forest polygon.

        When config.forests.KNOWN_FOREST_PREFABS has a confirmed FG_*.et path
        for the polygon's forest type (coniferous / deciduous / mixed / scrub /
        heath), a ForestGeneratorEntity child is auto-attached using the same
        ${guid}path.et { coords 0 0 0 } pattern established for roads (v1.1.0)
        and buildings (v1.2.0).

        The catalog ships empty (never fabricate paths). Once a user or
        contributor adds confirmed entries, the generator auto-upgrades those
        forest types to auto-attached mode on the next generation — no code
        change required.
        """
        header = (
            "// Vegetation layer — one closed spline per forest polygon.\n"
            "// Forest Generator prefab auto-attached when a confirmed FG_*.et path\n"
            "// exists in config/forests.py::KNOWN_FOREST_PREFABS for the forest type.\n"
            "// Catalog ships empty — add entries to upgrade from manual to auto mode.\n"
            "// Until populated: drag a Forest Generator from\n"
            "//   Prefabs/WEGenerators/Forest/ onto each spline manually.\n"
            "// Enable 'Avoid Roads' and 'Avoid Lakes' on the generator.\n"
            "// Source data: Reference/osm_forests.geojson and features.json.\n"
        )

        def _forest_child(feature: dict) -> Optional[str]:
            props = feature.get("properties", {}) or {}
            ft = forest_type_from_osm(props)
            path = validate_forest_prefab(ft)
            return path  # None → spline-only fallback

        body = self._polygon_features_to_splines(
            self.forest_features,
            entity_prefix="ForestArea",
            empty_message="// No forest polygons found in this area.\n",
            child_prefab_fn=_forest_child,
        )
        return header + body

    def _generate_water_layer(self) -> str:
        """
        Emit closed SplineShapeEntity per lake/pond/reservoir polygon AND open
        SplineShapeEntity per river/stream LineString.

        Lakes: when config.lakes.KNOWN_LAKE_PREFABS has a confirmed LG_*.et
        path for the water_type, a LakeGeneratorEntity child is auto-attached.
        Rivers: always emitted as open splines (no generator child — river
        generator paths TBD); the width comment shows an OSM-derived estimate.

        The lake catalog ships empty (same pattern as forests/buildings).
        """
        header = (
            "// Water layer — lakes/ponds/reservoirs as closed splines,\n"
            "// rivers/streams/canals as open splines.\n"
            "// Lake Generator prefab auto-attached when a confirmed LG_*.et path\n"
            "// exists in config/lakes.py::KNOWN_LAKE_PREFABS for the water type.\n"
            "// Catalog ships empty — add entries to upgrade from manual to auto mode.\n"
            "// Until populated: drag a Lake Generator from\n"
            "//   Prefabs/WEGenerators/Water/Lake/ onto each lake spline manually.\n"
            "// Enable 'Flatten By Bottom Plane' for natural water level.\n"
            "// Source data: Reference/osm_water.geojson and features.json.\n"
        )

        def _lake_child(feature: dict) -> Optional[str]:
            water_type = (feature.get("properties", {}) or {}).get("water_type", "")
            return validate_lake_prefab(water_type)  # None → spline-only fallback

        lake_body = self._polygon_features_to_splines(
            self.water_features,
            entity_prefix="Water",
            filter_property="water_type",
            filter_values=("lake", "pond", "reservoir", "water"),
            empty_message="// No standing-water polygons found in this area.\n",
            child_prefab_fn=_lake_child,
        )
        river_body = self._generate_river_splines()
        return header + lake_body + river_body

    def _generate_river_splines(self) -> str:
        """
        Emit one open SplineShapeEntity per river/stream/canal LineString.

        Rivers are open (not closed) so they don't need to form a polygon.
        A width comment shows the OSM-derived estimate in metres. No generator
        child is auto-attached — river generator prefab paths are TBD.
        """
        if not self.transformer or not self.water_features:
            return ""

        features = self.water_features.get("features", []) or []
        river_features = [
            f for f in features
            if (f.get("properties", {}) or {}).get("water_type", "") in _RIVER_WIDTH_M
            and (f.get("geometry", {}) or {}).get("type") == "LineString"
        ]
        if not river_features:
            return ""

        section_header = (
            "\n// River / stream splines — open SplineShapeEntity per waterway.\n"
            "// Width estimate (metres) shown in the entity comment.\n"
            "// Add a river generator child manually if your project uses one.\n"
        )

        entities: list[str] = []
        skipped = 0

        for i, feature in enumerate(river_features):
            props = (feature.get("properties", {}) or {})
            geom = (feature.get("geometry", {}) or {})
            coords = geom.get("coordinates", []) or []

            wgs_points = [
                {"x": float(c[0]), "y": float(c[1])}
                for c in coords if len(c) >= 2
            ]
            if len(wgs_points) < 2:
                skipped += 1
                continue

            local_points = self.transformer.transform_points(
                wgs_points,
                elevation_array=self.elevation_array,
            )

            margin = 1.0
            in_bounds = [
                pt for pt in local_points
                if -margin <= pt["x"] <= self.terrain_width + margin
                and -margin <= pt["z"] <= self.terrain_depth + margin
            ]
            if len(in_bounds) < 2:
                skipped += 1
                continue

            origin = in_bounds[0]
            point_defs = []
            for j, pt in enumerate(in_bounds):
                rel_x = pt["x"] - origin["x"]
                rel_y = pt["y"] - origin["y"]
                rel_z = pt["z"] - origin["z"]
                point_defs.append(
                    f'   ShapePoint sp_{j} {{\n'
                    f'    Position {rel_x:.3f} {rel_y:.3f} {rel_z:.3f}\n'
                    f'   }}'
                )

            water_type = props.get("water_type", "river")
            width_m = _RIVER_WIDTH_M.get(water_type, 5.0)
            name = props.get("name", "") or ""
            comment = (
                f' // {name} (~{width_m:.0f}m)' if name
                else f' // {water_type} (~{width_m:.0f}m)'
            )

            entities.append(
                f'SplineShapeEntity River_{i} {{{comment}\n'
                f' coords {origin["x"]:.3f} {origin["y"]:.3f} {origin["z"]:.3f}\n'
                f' Points {{\n'
                + "\n".join(point_defs) + "\n"
                f' }}\n'
                f'}}'
            )

        if skipped:
            logger.info(
                f"River splines: skipped {skipped} segment(s) — "
                f"< 2 in-bounds points after clipping"
            )
        logger.info(f"River splines: emitted {len(entities)} open spline(s)")

        if not entities:
            return ""
        return section_header + "\n".join(entities) + "\n"

    # -----------------------------------------------------------------------
    # Buildings layer (Phase 2 / task A2 + L12)
    # -----------------------------------------------------------------------

    def _generate_buildings_layer(self) -> str:
        """
        Emit one entity per extracted building.

        Two emission modes per building, decided by whether
        ``config.buildings.KNOWN_BUILDING_PREFABS`` has a verified Enfusion
        prefab path for the building's category:

        * **Validated prefab** → emit a positioned prefab instance using the
          ``${guid}path/to/prefab.et { coords X Y Z }`` syntax (the same
          pattern the managers / roads layers use successfully).
        * **No validated prefab** (the default until the user confirms paths
          for a stock Reforger install) → emit a closed-spline footprint
          marker on the building's exterior ring. The user sees the actual
          building outline in the World Editor and drags a prefab onto it.

        Buildings whose centroid falls inside the asphalt-road exclusion
        zone (half road width + 1.5 m safety) are dropped (audit task L12)
        — placing buildings on top of a generated road would produce visible
        Z-fighting and block traffic.
        """
        header = (
            "// Buildings layer — one entity per extracted OSM building.\n"
            "// Buildings with a verified Enfusion prefab in\n"
            "//   config/buildings.py::KNOWN_BUILDING_PREFABS\n"
            "// are emitted as auto-positioned prefab instances. Buildings\n"
            "// whose category has no verified prefab are emitted as closed\n"
            "// footprint splines — drag a Building_*.et prefab from\n"
            "// Prefabs/Structures/ onto each spline to wire it up.\n"
            "// Buildings overlapping asphalt roads are dropped (L12).\n"
            "// Source data: Reference/osm_buildings.geojson and features.json.\n"
        )

        if not self.transformer:
            return header + "// (Coordinate transformer unavailable — buildings cannot be placed.)\n"
        if not self.building_data:
            return header + "// No building data available.\n"

        buildings = self.building_data.get("buildings", [])
        if not buildings:
            return header + "// No buildings found in this area.\n"

        exclusion_zone = self._build_road_exclusion_zone()

        entities: list[str] = []
        skipped_road_overlap = 0
        skipped_out_of_bounds = 0
        prefab_count = 0
        marker_count = 0

        for building in buildings:
            center_lonlat = building.get("center")
            if not center_lonlat or len(center_lonlat) < 2:
                continue
            lon, lat = float(center_lonlat[0]), float(center_lonlat[1])

            # L12 — drop buildings that would sit on top of an asphalt road.
            if exclusion_zone is not None and self._point_inside_geometry(
                lon, lat, exclusion_zone
            ):
                skipped_road_overlap += 1
                continue

            # Transform the centroid to terrain-local coords (with elevation
            # sampling) for positioning the entity.
            local_pts = self.transformer.transform_points(
                [{"x": lon, "y": lat, "z": 0}],
                elevation_array=self.elevation_array,
            )
            if not local_pts:
                skipped_out_of_bounds += 1
                continue
            origin = local_pts[0]
            margin = 1.0
            if not (
                -margin <= origin["x"] <= self.terrain_width + margin
                and -margin <= origin["z"] <= self.terrain_depth + margin
            ):
                skipped_out_of_bounds += 1
                continue

            prefab_path = building.get("enfusion_prefab")
            building_name_raw = building.get("name", "") or ""
            building_name = building_name_raw.replace('"', "'")
            building_type = building.get("building_type", "yes")
            comment = (
                f' // {building_name} ({building_type})'
                if building_name
                else f' // {building_type}'
            )

            if prefab_path:
                prefab_ref = f"${{{ARMA_REFORGER_GUID}}}{prefab_path}"
                entities.append(
                    f'{prefab_ref} {{{comment}\n'
                    f' coords {origin["x"]:.3f} {origin["y"]:.3f} {origin["z"]:.3f}\n'
                    f'}}'
                )
                prefab_count += 1
            else:
                # Footprint marker: emit the exterior ring as a closed spline
                # the user can right-click → Add Child Entity → BuildingEntity.
                geom = building.get("geometry") or {}
                ring = self._building_exterior_ring(geom)
                if ring is None:
                    # Fall back to a tiny spline at the centroid so the
                    # building still appears in the editor hierarchy.
                    ring = [
                        [lon - 0.00001, lat - 0.00001],
                        [lon + 0.00001, lat - 0.00001],
                        [lon + 0.00001, lat + 0.00001],
                        [lon - 0.00001, lat + 0.00001],
                        [lon - 0.00001, lat - 0.00001],
                    ]
                entity = self._closed_spline_entity_from_ring(
                    ring, "Building", len(entities), comment_suffix=comment
                )
                if entity is None:
                    skipped_out_of_bounds += 1
                    continue
                entities.append(entity)
                marker_count += 1

        if skipped_road_overlap:
            logger.info(
                f"Buildings: dropped {skipped_road_overlap} that overlapped "
                f"asphalt roads (L12 de-conflict)"
            )
        if skipped_out_of_bounds:
            logger.info(
                f"Buildings: dropped {skipped_out_of_bounds} that were "
                f"outside terrain bounds or had no usable footprint"
            )
        logger.info(
            f"Buildings layer: {prefab_count} auto-placed prefab instance(s), "
            f"{marker_count} footprint marker(s)"
        )

        if not entities:
            return header + "// No buildings remained after L12 filtering.\n"
        return header + "\n".join(entities) + "\n"

    def _build_road_exclusion_zone(self):
        """
        Build a single shapely geometry covering all asphalt road centerlines
        buffered by half the road width plus a 1.5 m safety margin (L12).

        Returns ``None`` if shapely is unavailable, no road data is provided,
        or there are no asphalt roads in the area.
        """
        if not self.road_data:
            return None
        try:
            from shapely.geometry import LineString
            from shapely.ops import unary_union
        except ImportError:  # pragma: no cover - shapely is a hard dep
            logger.warning(
                "shapely not available — skipping building/road L12 de-conflict"
            )
            return None

        # Convert meter buffer widths to degrees using the area centroid's
        # latitude. Errs on the larger-buffer side by dividing by the smaller
        # m_per_deg value (longitude shrinks toward the poles).
        bbox = self.metadata.get("input", {}).get("bbox", {}) or {}
        center_lat = (bbox.get("south", 0) + bbox.get("north", 0)) / 2
        m_per_deg_lat = 110540.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians(center_lat or 0.0))
        smaller_m_per_deg = min(m_per_deg_lat, max(m_per_deg_lon, 1.0))
        deg_per_m = 1.0 / smaller_m_per_deg

        safety_margin_m = 1.5
        buffered: list = []
        for road in self.road_data.get("roads", []) or []:
            if road.get("surface") != "asphalt":
                continue
            pts = road.get("spline_points", [])
            if len(pts) < 2:
                continue
            try:
                line = LineString([(float(p["x"]), float(p["y"])) for p in pts])
            except (KeyError, ValueError, TypeError):
                continue
            try:
                width_m = float(road.get("width_m", 4))
            except (TypeError, ValueError):
                width_m = 4.0
            half_width_deg = (width_m / 2 + safety_margin_m) * deg_per_m
            buffered.append(line.buffer(half_width_deg))

        if not buffered:
            return None
        return unary_union(buffered)

    @staticmethod
    def _point_inside_geometry(lon: float, lat: float, geometry) -> bool:
        """Cheap point-in-geometry test using shapely."""
        try:
            from shapely.geometry import Point
        except ImportError:  # pragma: no cover
            return False
        try:
            return geometry.contains(Point(lon, lat))
        except Exception:  # pragma: no cover - defensive
            return False

    @staticmethod
    def _building_exterior_ring(geom: dict) -> Optional[list]:
        """Return the exterior ring of a Polygon or first-MultiPolygon geometry."""
        if not geom:
            return None
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        if gtype == "Polygon" and coords:
            return coords[0]
        if gtype == "MultiPolygon" and coords and coords[0]:
            return coords[0][0]
        return None

    def _polygon_features_to_splines(
        self,
        features: Optional[dict],
        entity_prefix: str,
        empty_message: str,
        filter_property: Optional[str] = None,
        filter_values: tuple[str, ...] = (),
        child_prefab_fn=None,
    ) -> str:
        """
        Convert GeoJSON polygon features into closed SplineShapeEntity blocks.

        Each Polygon emits one entity from its exterior ring; each MultiPolygon
        emits one entity per sub-polygon's exterior ring. Interior rings (holes)
        are dropped — closed splines can't represent them, and the user can
        manually mask out island areas if needed.

        Points are projected from WGS84 to local terrain coordinates via the
        same transformer + elevation_array path used for road splines, then
        clipped to the terrain bounds. A polygon is skipped if fewer than 3
        in-bounds points remain (degenerate / fully outside).

        Args:
            features: GeoJSON FeatureCollection or None.
            entity_prefix: Per-entity name prefix, e.g. "ForestArea" → ForestArea_0.
            empty_message: Comment to emit when there's nothing to write.
            filter_property: Optional GeoJSON property name to filter on.
            filter_values: Allowed values for filter_property (case-sensitive).
            child_prefab_fn: Optional callable(feature) → str|None. When it
                returns a non-None path, a generator child entity is added to
                the spline using the ${guid}path.et { coords 0 0 0 } syntax.

        Returns:
            Multi-line string suitable for appending to a .layer file.
        """
        if not self.transformer:
            return "// (Coordinate transformer unavailable — splines cannot be projected.)\n"
        if not features or not features.get("features"):
            return empty_message

        entities: list[str] = []
        skipped_clipped = 0
        skipped_filtered = 0

        for feature in features["features"]:
            if filter_property:
                value = feature.get("properties", {}).get(filter_property, "")
                if value not in filter_values:
                    skipped_filtered += 1
                    continue

            child_prefab = child_prefab_fn(feature) if child_prefab_fn else None

            geom = feature.get("geometry", {}) or {}
            geom_type = geom.get("type", "")
            coords = geom.get("coordinates", []) or []

            # Collect every exterior ring this feature contributes
            exterior_rings: list[list[list[float]]] = []
            if geom_type == "Polygon" and coords:
                exterior_rings.append(coords[0])
            elif geom_type == "MultiPolygon" and coords:
                for polygon in coords:
                    if polygon:
                        exterior_rings.append(polygon[0])
            else:
                continue  # Lines / points etc. — not handled here

            for ring in exterior_rings:
                entity = self._closed_spline_entity_from_ring(
                    ring, entity_prefix, len(entities),
                    child_prefab=child_prefab,
                )
                if entity is None:
                    skipped_clipped += 1
                    continue
                entities.append(entity)

        if skipped_clipped:
            logger.info(
                f"{entity_prefix}: skipped {skipped_clipped} polygon(s) — "
                f"fewer than 3 in-bounds points after clipping"
            )
        if skipped_filtered:
            logger.info(
                f"{entity_prefix}: filtered out {skipped_filtered} feature(s) "
                f"by {filter_property}"
            )
        logger.info(
            f"{entity_prefix}: emitted {len(entities)} closed spline entit(y/ies)"
        )

        if not entities:
            return empty_message
        return "\n".join(entities) + "\n"

    def _closed_spline_entity_from_ring(
        self,
        ring: list[list[float]],
        entity_prefix: str,
        index: int,
        comment_suffix: str = "",
        child_prefab: Optional[str] = None,
    ) -> Optional[str]:
        """
        Project a single GeoJSON ring (list of [lon, lat] pairs) to a closed
        SplineShapeEntity. Returns None if the ring has fewer than 3 in-bounds
        points after clipping to the terrain.

        ``comment_suffix`` is appended to the entity declaration line as a
        trailing ``// ...`` comment so callers can label the spline.

        ``child_prefab``: when not None, a generator child block is appended
        inside the entity using the ``${guid}path.et { coords 0 0 0 }`` syntax
        established for roads (v1.1.0) and buildings (v1.2.0).
        """
        if len(ring) < 3:
            return None

        # GeoJSON rings are typically explicitly closed (first==last). Drop the
        # duplicate last point before projection; we'll re-close the spline below.
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring = ring[:-1]

        wgs_points = [{"x": float(p[0]), "y": float(p[1])} for p in ring if len(p) >= 2]
        if len(wgs_points) < 3:
            return None

        local_points = self.transformer.transform_points(
            wgs_points,
            elevation_array=self.elevation_array,
        )

        # Clip to terrain bounds with a small margin
        margin = 1.0
        in_bounds = [
            pt for pt in local_points
            if -margin <= pt["x"] <= self.terrain_width + margin
            and -margin <= pt["z"] <= self.terrain_depth + margin
        ]
        if len(in_bounds) < 3:
            return None

        # Close the spline: append a final ShapePoint that repeats the origin
        local_points = in_bounds + [in_bounds[0]]

        origin = local_points[0]
        point_defs = []
        for j, pt in enumerate(local_points):
            rel_x = pt["x"] - origin["x"]
            rel_y = pt["y"] - origin["y"]
            rel_z = pt["z"] - origin["z"]
            point_defs.append(
                f'   ShapePoint sp_{j} {{\n'
                f'    Position {rel_x:.3f} {rel_y:.3f} {rel_z:.3f}\n'
                f'   }}'
            )

        child_block = ""
        if child_prefab:
            child_block = (
                f' ${{{ARMA_REFORGER_GUID}}}{child_prefab} {{\n'
                f'  coords 0 0 0\n'
                f' }}\n'
            )

        return (
            f'SplineShapeEntity {entity_prefix}_{index} {{{comment_suffix}\n'
            f' coords {origin["x"]:.3f} {origin["y"]:.3f} {origin["z"]:.3f}\n'
            f' Points {{\n'
            + "\n".join(point_defs) + "\n"
            f' }}\n'
            + child_block
            + f'}}'
        )

    def _generate_mission_conf(self) -> str:
        """Generate mission header .conf file."""
        return f'''SCR_MissionHeader {{
 World "{{{self.project_guid}}}Worlds/{self.map_name}.ent"
 m_sName "{self.map_name}"
 m_sGameMode "GameMaster"
 m_iPlayerCount 64
}}
'''
