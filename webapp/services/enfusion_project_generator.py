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

from config.enfusion import (
    ARMA_REFORGER_GUID,
    PLATFORM_CONFIGS,
    WORLD_ENTITY_DEFAULTS,
    TERRAIN_LOD_DEFAULTS,
    WORLD_PREFABS,
    ROAD_PREFAB_BASE,
    PROJECT_NAME_ALLOWED_CHARS,
    PROJECT_NAME_MAX_LENGTH,
    compute_height_scale,
)

logger = logging.getLogger(__name__)


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
    ):
        """
        Initialize the project generator.

        Args:
            map_name: User-provided map name (will be sanitized).
            metadata: Full map generation metadata dict.
            road_data: Optional processed road data for road layer generation.
            transformer: Optional CoordinateTransformer for road coordinate conversion.
        """
        self.map_name = sanitize_project_name(map_name)
        self.metadata = metadata
        self.road_data = road_data
        self.transformer = transformer

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
            self._generate_placeholder_layer("vegetation")
        )

        files["water.layer"] = self._write_file(
            worlds_dir / f"{self.map_name}_water.layer",
            self._generate_placeholder_layer("water")
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
        Generate the roads layer with SplineShapeEntity + RoadGeneratorEntity entries.

        If road_data and transformer are available, generates actual road entities.
        Otherwise generates an empty layer with a comment.

        Entity format (matching Enfusion World Editor conventions):
          SplineShapeEntity Road_N {
           coords X 0 Z
           Points {
            ShapePoint sp_0 { Position 0 0 0 }
            ShapePoint sp_1 { Position relX 0 relZ }
            ...
           }
           {
            RoadGeneratorEntity : "{GUID}Prefabs/..." { AdjustHeightMap 1 }
           }
          }

        Road entities are clipped to terrain bounds — roads with origin
        outside the terrain or fewer than 2 points within bounds are skipped.
        """
        if not self.road_data or not self.transformer:
            return '// Road layer — no road data available or coordinate transformer not configured.\n// Use Reference/roads_enfusion.geojson to manually place roads.\n'

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

            # Transform points to local coordinates
            local_points = self.transformer.transform_points(points)

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

            # Skip if origin is outside terrain bounds
            if (origin["x"] < -margin or origin["x"] > self.terrain_width + margin
                    or origin["z"] < -margin or origin["z"] > self.terrain_depth + margin):
                clipped += 1
                continue

            prefab = road.get("enfusion_prefab", "RG_Road_Asphalt_4m")

            # Build ShapePoint definitions (relative to entity origin)
            # Wrapped in a Points {} block per Enfusion format
            point_defs = []
            for j, pt in enumerate(local_points):
                rel_x = pt["x"] - origin["x"]
                rel_z = pt["z"] - origin["z"]
                point_defs.append(
                    f'   ShapePoint sp_{j} {{\n'
                    f'    Position {rel_x:.3f} 0 {rel_z:.3f}\n'
                    f'   }}'
                )

            road_name = road.get("name", "").replace('"', "'")
            if road_name:
                comment = f' // {road_name}'
            else:
                comment = ""

            entity = (
                f'SplineShapeEntity Road_{i} {{{comment}\n'
                f' coords {origin["x"]:.3f} 0 {origin["z"]:.3f}\n'
                f' Points {{\n'
                + "\n".join(point_defs) + "\n"
                f' }}\n'
                f' {{\n'
                f'  RoadGeneratorEntity : "{{58D0FB3206B6F859}}{ROAD_PREFAB_BASE}/{prefab}.et" {{\n'
                f'   AdjustHeightMap 1\n'
                f'  }}\n'
                f' }}\n'
                f'}}'
            )
            entities.append(entity)

        if skipped > 0:
            logger.info(f"Skipped {skipped} road segments with < 2 points")
        if clipped > 0:
            logger.info(f"Clipped {clipped} road segments outside terrain bounds")

        logger.info(f"Generated {len(entities)} road entities for roads layer")
        return "\n".join(entities) + "\n"

    def _generate_placeholder_layer(self, layer_type: str) -> str:
        """Generate a placeholder layer with a descriptive comment."""
        comments = {
            "vegetation": (
                "// Vegetation layer — placeholder for Forest Generator entities.\n"
                "// Use Reference/osm_forests.geojson and features.json for forest placement.\n"
                "// Forest Generator prefabs: Prefabs/WEGenerators/Forest/ (FG_ prefix)\n"
            ),
            "water": (
                "// Water layer — placeholder for Lake/River Generator entities.\n"
                "// Use Reference/osm_water.geojson and features.json for water placement.\n"
                "// Lake Generator prefabs: Prefabs/WEGenerators/Water/Lake/ (LG_ prefix)\n"
            ),
        }
        return comments.get(layer_type, f"// {layer_type} layer — placeholder\n")

    def _generate_mission_conf(self) -> str:
        """Generate mission header .conf file."""
        return f'''SCR_MissionHeader {{
 World "{{{self.project_guid}}}Worlds/{self.map_name}.ent"
 m_sName "{self.map_name}"
 m_sGameMode "GameMaster"
 m_iPlayerCount 64
}}
'''
