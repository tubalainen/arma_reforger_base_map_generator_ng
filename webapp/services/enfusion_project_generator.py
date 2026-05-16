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
    APP_VERSION,
    ARMA_REFORGER_GUID,
    PLATFORM_CONFIGS,
    FOREST_PREFAB_BASE,
    LAKE_PREFAB_BASE,
    WORLD_ENTITY_DEFAULTS,
    TERRAIN_LOD_DEFAULTS,
    WORLD_PREFABS,
    PROJECT_NAME_ALLOWED_CHARS,
    PROJECT_NAME_MAX_LENGTH,
    MAX_SPLINE_POINTS,
    MAX_SPLINE_POINTS_NATURAL,
    MANDATORY_BOOTSTRAP_KEYS,
    resolve_ambient_prefab,
    compute_height_scale,
)
from config.roads import validate_road_prefab, fully_qualified_road_prefab
from config.forests import validate_forest_prefab, forest_type_from_osm
from config.lakes import validate_lake_prefab
from services.entity_naming import EntityNamer, expected_surface
from services.spline_cleanup import (
    normalize_polygons,
    normalize_polylines,
    adaptive_tolerance,
)

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
        country_codes: Optional[list[str]] = None,
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
        # Pick the AmbientSounds_*.et variant from the detected country list,
        # falling back to the metadata's input.countries if not passed directly.
        if country_codes is None:
            country_codes = (
                self.metadata.get("input", {}).get("countries", []) or []
            )
        self.country_codes = list(country_codes)
        self.ambient_prefab = resolve_ambient_prefab(self.country_codes)
        # surface_assignments accumulated as splines are emitted, then written
        # to surface_assignments.json by the map_generator pipeline.
        self.surface_assignments: dict[str, str] = {}
        # Reset on each `generate_all` so a generator reused across requests
        # never carries naming state forward — see _reset_naming_state().
        self._namer: Optional[EntityNamer] = None

        # Addon GUID — identifies this project in addon.gproj and dependency
        # lists. The world.ent and mission.conf each need their own unique
        # GUID; reusing project_guid for both caused "duplicate GUID"
        # registration errors in Workbench (issue #61).
        self.project_guid = generate_guid(self.map_name)
        self.world_ent_guid = generate_guid(self.map_name + ":world.ent")
        self.mission_conf_guid = generate_guid(self.map_name + ":mission.conf")

        # Extract key values from metadata (terrain dims, elevation, location).
        self._extract_terrain_params()

        logger.info(
            f"EnfusionProjectGenerator initialized: name={self.map_name}, "
            f"guid={self.project_guid}, faces={self.face_count_x}x{self.face_count_z}"
        )

    def _reset_naming_state(self) -> None:
        """Re-create the EntityNamer + clear surface assignments."""
        self._namer = EntityNamer(self.terrain_width, self.terrain_depth)
        self.surface_assignments = {}

    def _record_surface(self, entity_name: str, surface: Optional[str]) -> None:
        if surface:
            self.surface_assignments[entity_name] = surface

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

        # Fresh per-call naming state so two consecutive generations from the
        # same generator never share name counters / collision tracking.
        self._reset_naming_state()

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
            self._generate_meta("ent", f"Worlds/{self.map_name}.ent", self.world_ent_guid)
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
            self._generate_meta("conf", f"Missions/{self.map_name}.conf", self.mission_conf_guid)
        )

        logger.info(f"Generated {len(files)} Enfusion project files in {output_dir}")
        if job:
            job.add_log(f"Generated {len(files)} Enfusion project files", "success")
        return files

    # Stamp every Enfusion text file with the generator version (issue #99)
    # so users hunting crashes can tell which release produced their map.
    # `.meta` and `.conf` are skipped because we haven't confirmed Enfusion's
    # parser tolerates leading `//` comments there — the .layer/.ent/.gproj
    # files are all C-syntax with verified comment support.
    _GENERATOR_BANNER = (
        f"// Generated by Arma Reforger Base Map Generator v{APP_VERSION}\n"
        f"// https://github.com/tubalainen/arma_reforger_base_map_generator_ng\n"
    )

    def _write_file(self, path: Path, content: str) -> str:
        """Write content to file and return the path as string.

        Prepends the generator-version banner to every C-syntax Enfusion
        text file (.gproj/.ent/.layer). `.meta` and `.conf` are passed
        through untouched.
        """
        suffix = path.suffix.lower()
        if suffix in (".gproj", ".ent", ".layer"):
            content = self._GENERATOR_BANNER + content
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

    def _generate_meta(self, resource_type: str, resource_path: str, guid: str) -> str:
        """
        Generate a .meta file for a resource.

        Args:
            resource_type: "ent" or "conf"
            resource_path: Relative path to the resource file.
            guid: Unique 16-char hex GUID for this specific resource.
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
 Name "{{{guid}}}{resource_path}"
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
        """
        Generate the managers layer with the full Atlas 2 bootstrap entity set.

        Emits every key from MANDATORY_BOOTSTRAP_KEYS (camera, weather, audio,
        destruction, MP destruction, preload, radio, music, forest sync,
        projectile sounds, map entity) plus the country-resolved AmbientSounds
        prefab. v1.4.0 added the four MP / music / preload / radio entries to
        close the gap against the manual "Atlas 2" workflow (issue #81).
        """
        center_x = self.terrain_width / 2
        center_z = self.terrain_depth / 2
        camera_y = self.max_elevation + 200

        header = (
            f"// Managers layer — bootstrap entities required for a fully\n"
            f"// functional Reforger world (Atlas 2 alignment, v1.4.0).\n"
            f"// Entries: {', '.join(MANDATORY_BOOTSTRAP_KEYS)}\n"
            f"// Ambient sounds: resolved to {self.ambient_prefab}\n"
            f"//   (countries detected: {', '.join(self.country_codes) or 'none'})\n"
        )

        guid = ARMA_REFORGER_GUID
        parts = [
            f'${{{guid}}}{WORLD_PREFABS["camera"]} {{\n'
            f' coords {center_x:.1f} {camera_y:.1f} {center_z:.1f}\n'
            f' PlayFromCameraPosition 1\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["time_weather"]} {{\n'
            f' coords 0 0 0\n'
            f' Latitude {self.center_lat:.4f}\n'
            f' Longitude {self.center_lon:.4f}\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["projectile_sounds"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["map_entity"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["sound_world"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["forest_sync"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["destruction"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["mp_destruction"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["preload"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["radio_broadcast"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{WORLD_PREFABS["music_manager"]} {{\n'
            f' coords 0 0 0\n'
            f'}}',
            f'${{{guid}}}{self.ambient_prefab} {{ // biome-matched ambient sound\n'
            f' coords 0 0 0\n'
            f'}}',
        ]

        return header + "\n".join(parts) + "\n"

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
        Generate the roads layer with one spline-only SplineShapeEntity per road.

        v1.1.0 attempted to auto-attach a ``RoadGeneratorEntity`` child by
        nesting a ``${guid}path/to/prefab.et { coords 0 0 0 }`` instance line
        inside the SplineShapeEntity body. Workbench rejects that nesting
        form and stalls "Loading entity data..." at 4%. Reverted in v1.2.3.

        The validated prefab name is still computed (via ``validate_road_prefab``)
        and surfaced in the entity's trailing ``//`` comment so the user can
        attach the right ``RoadGeneratorEntity`` child manually in Workbench.
        ``Reference/roads_reference.csv`` carries the same data per road.

        Spline points include Y (elevation) values sampled from the heightmap
        so roads follow the terrain surface, and are simplified to at most
        ``MAX_SPLINE_POINTS`` vertices to avoid choking the Workbench loader
        on long OSM ways.

        Entity format:
          SplineShapeEntity Road_N { // <name> | prefab: RG_Road_<Surface>_<W>m
           coords X Y Z
           Points {
            ShapePoint sp_0 { Position 0 0 0 }
            ShapePoint sp_1 { Position relX relY relZ }
            ...
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

            # Cap spline length so long OSM ways don't hang the Workbench
            # loader. Roads keep the looser MAX_SPLINE_POINTS budget because
            # their geometry is dictated by real-world surveys (natural
            # forest/lake/river splines use the tighter NATURAL cap — v1.4.4).
            local_points = self._simplify_local_polyline(
                in_bounds, max_pts=MAX_SPLINE_POINTS
            )

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
            prefab_name = validate_road_prefab(
                road.get("enfusion_prefab", "")
            )

            # Descriptive entity name (v1.4.0 — Atlas 2 alignment).
            namer_props = {
                "ref": road.get("ref", ""),
                "name": road_name,
                "surface": road.get("surface", "asphalt"),
            }
            entity_name = self._namer.make_name(
                "Road",
                properties=namer_props,
                x_local=origin["x"],
                z_local=origin["z"],
            )
            paints = expected_surface("Road", namer_props)
            self._record_surface(entity_name, paints)

            comment_parts = []
            if road_name:
                comment_parts.append(road_name)
            comment_parts.append(f"prefab: {prefab_name}")
            if paints:
                comment_parts.append(f"paints: {paints}")
            # When the prefab is in the Atlas 2 catalogue, also surface its
            # fully-qualified `{guid}path.et` form so the editor user can
            # paste it directly into the RoadGeneratorEntity Prefab field
            # (saves a Resource Browser search).
            fq = fully_qualified_road_prefab(prefab_name)
            if fq:
                comment_parts.append(f"fq: {fq}")
            comment = " // " + " | ".join(comment_parts)

            entity = (
                f'SplineShapeEntity {entity_name} {{{comment}\n'
                f' coords {origin["x"]:.3f} {origin["y"]:.3f} {origin["z"]:.3f}\n'
                f' Points {{\n'
                + "\n".join(point_defs) + "\n"
                f' }}\n'
                f'}}'
            )
            entities.append(entity)

        if skipped > 0:
            logger.info(f"Skipped {skipped} road segments with < 2 points")
        if clipped > 0:
            logger.info(f"Clipped {clipped} road segments outside terrain bounds")

        logger.info(
            f"Generated {len(entities)} spline-only road entities for roads "
            f"layer (prefab name surfaced as a // comment per spline)"
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

        # v1.4.0 — naming_kind="Forest" tells the namer to produce
        # `Forest_Pine_NE_001` / `Forest_Deciduous_SW_004` style names
        # instead of the legacy `ForestArea_<idx>` sequence. The OSM
        # `forest_type` property is added to feature properties below.
        for feature in (self.forest_features or {}).get("features", []) or []:
            props = feature.setdefault("properties", {})
            if "forest_type" not in props:
                props["forest_type"] = forest_type_from_osm(props)

        # v1.4.4 — collapse duplicate / overlapping forests via shapely
        # unary_union BEFORE projection (issues #93, #88). Way+relation
        # versions of the same wood merge; adjacent touching forests
        # become one continuous spline.
        cleaned_forests = self._normalized_polygon_collection(
            self.forest_features, "forest"
        )

        body = self._polygon_features_to_splines(
            cleaned_forests,
            entity_prefix="ForestArea",
            empty_message="// No forest polygons found in this area.\n",
            child_prefab_fn=_forest_child,
            naming_kind="Forest",
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

        # v1.4.4 — dedup/union lake polygons before projection (issues #93,
        # #88). Coastlines + rivers pass through unchanged; polygon water
        # features (lake / pond / reservoir / water) get merged.
        cleaned_water = self._normalized_polygon_collection(
            self.water_features, "lake"
        )

        lake_body = self._polygon_features_to_splines(
            cleaned_water,
            entity_prefix="Water",
            filter_property="water_type",
            filter_values=("lake", "pond", "reservoir", "water"),
            empty_message="// No standing-water polygons found in this area.\n",
            child_prefab_fn=_lake_child,
            naming_kind="Lake",
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

        # v1.4.4 — drop hairpin (>150° near-reversal in <20 m) vertices on
        # rivers BEFORE projection. This kills the spiral / loop artefact
        # reported in #93 (e.g. the "crazy looped spline" screenshot).
        river_features = normalize_polylines(river_features, "river")
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

            in_bounds = self._simplify_local_polyline(in_bounds)

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

            namer_props = {"name": name, "water_type": water_type}
            entity_name = self._namer.make_name(
                "River",
                properties=namer_props,
                x_local=origin["x"],
                z_local=origin["z"],
            )
            paints = expected_surface("River", namer_props)
            self._record_surface(entity_name, paints)

            label = name if name else water_type
            comment_parts = [f"{label} (~{width_m:.0f}m)"]
            if paints:
                comment_parts.append(f"paints: {paints}")
            comment = " // " + " | ".join(comment_parts)

            entities.append(
                f'SplineShapeEntity {entity_name} {{{comment}\n'
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
            "// Buildings layer — one positioned prefab instance per OSM building.\n"
            "// Each entity references a verified Building_*.et from\n"
            "//   config/buildings.py::KNOWN_BUILDING_PREFABS\n"
            "// and is rotated by `angles 0 <yaw> 0` to align the building's\n"
            "// longest wall with the OSM footprint orientation. Buildings\n"
            "// with an uncatalogued category are logged and skipped (the\n"
            "// previous spline-footprint fallback was removed in v1.4.6\n"
            "// because nested-child syntax inside SplineShapeEntity has\n"
            "// hung Workbench at 4% on world load — see issue #85).\n"
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

            namer_props = {"name": building_name, "building_type": building_type}
            entity_name = self._namer.make_name(
                "Building",
                properties=namer_props,
                x_local=origin["x"],
                z_local=origin["z"],
            )
            comment = (
                f' // {entity_name} | {building_name} ({building_type})'
                if building_name
                else f' // {entity_name} | {building_type}'
            )

            if prefab_path:
                prefab_ref = f"${{{ARMA_REFORGER_GUID}}}{prefab_path}"
                rotation_deg = float(building.get("rotation_deg", 0.0) or 0.0)
                body_lines = [
                    f' coords {origin["x"]:.3f} {origin["y"]:.3f} {origin["z"]:.3f}',
                ]
                # `angles 0 <yaw> 0` form verified against shipped community
                # building layers (DarcMods town01.layer, Overthrow, Coalition).
                # Skip the line for cardinal-aligned buildings to match the
                # convention in those files — keeps the output diff-friendly.
                if abs(rotation_deg) > 0.05:
                    body_lines.append(f' angles 0 {rotation_deg:.2f} 0')
                entities.append(
                    f'{prefab_ref} {{{comment}\n'
                    + '\n'.join(body_lines)
                    + '\n}'
                )
                prefab_count += 1
            else:
                # Should be unreachable: every category extractor produces has
                # a KNOWN_BUILDING_PREFABS entry, and Building_Generic catches
                # building=yes. If this fires, the catalog and extractor have
                # diverged — fix config/buildings.py rather than emit a
                # SplineShapeEntity. Nested-child syntax inside one caused
                # Workbench to hang at 4% on world load in v1.1.0 (reverted
                # in v1.2.3) so we never re-arm that footgun.
                logger.warning(
                    f"Building category {building.get('prefab_category')!r} "
                    f"has no entry in KNOWN_BUILDING_PREFABS — skipping "
                    f"osm_id={building.get('osm_id')}. Add the category to "
                    f"config/buildings.py to fix."
                )
                marker_count += 1
                continue

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
    def _normalized_polygon_collection(
        features: Optional[dict],
        kind: str,
    ) -> Optional[dict]:
        """
        Run :func:`spline_cleanup.normalize_polygons` over a FeatureCollection,
        returning a new FeatureCollection with the same shape.

        Non-polygon members (e.g. river LineStrings inside the water feature
        set) pass through unchanged. ``None`` / empty input is returned as-is
        so callers can keep their existing empty-message handling.
        """
        if not features or not features.get("features"):
            return features
        cleaned = normalize_polygons(features["features"], kind)
        return {"type": "FeatureCollection", "features": cleaned}

    def _polygon_features_to_splines(
        self,
        features: Optional[dict],
        entity_prefix: str,
        empty_message: str,
        filter_property: Optional[str] = None,
        filter_values: tuple[str, ...] = (),
        child_prefab_fn=None,
        naming_kind: Optional[str] = None,
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

            feature_props = feature.get("properties", {}) or {}
            for ring in exterior_rings:
                entity = self._closed_spline_entity_from_ring(
                    ring, entity_prefix, len(entities),
                    child_prefab=child_prefab,
                    naming_kind=naming_kind,
                    feature_properties=feature_props,
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

    @staticmethod
    def _simplify_local_ring(
        pts: list[dict],
        max_pts: int = MAX_SPLINE_POINTS_NATURAL,
    ) -> list[dict]:
        """
        Reduce a local-coordinate ring to at most *max_pts* points using
        Ramer-Douglas-Peucker in the XZ plane, falling back to uniform
        decimation if shapely is unavailable or all tolerances fail.

        *pts* is a list of {"x":…, "y":…, "z":…} dicts (local metres, no
        closing duplicate).  The returned list has the same dict structure.

        v1.4.4 — simplification ALWAYS runs (previously bypassed for any ring
        ≤200 pts, which left most OSM rings over-detailed at 50–200 vertices).
        An :func:`adaptive_tolerance` scaled to the bbox diagonal is tried
        first; ``preserve_topology=True`` rules out self-intersecting outputs.
        """
        if len(pts) < 4:
            return pts

        def _rdp(points, tol):
            if len(points) <= 2:
                return list(points)
            ax, az = points[0]["x"], points[0]["z"]
            bx, bz = points[-1]["x"], points[-1]["z"]
            dx, dz = bx - ax, bz - az
            seg_len_sq = dx * dx + dz * dz

            def _perp(p):
                if seg_len_sq == 0:
                    return math.hypot(p["x"] - ax, p["z"] - az)
                t = max(0.0, min(1.0, ((p["x"] - ax) * dx + (p["z"] - az) * dz) / seg_len_sq))
                return math.hypot(p["x"] - (ax + t * dx), p["z"] - (az + t * dz))

            max_d, idx = 0.0, 0
            for i in range(1, len(points) - 1):
                d = _perp(points[i])
                if d > max_d:
                    max_d, idx = d, i
            if max_d > tol:
                return _rdp(points[:idx + 1], tol)[:-1] + _rdp(points[idx:], tol)
            return [points[0], points[-1]]

        # Build an adaptive tolerance schedule that starts at the size-scaled
        # value and escalates only as a safety net for pathological rings.
        base_tol = adaptive_tolerance(pts)
        tol_schedule = [base_tol]
        for extra in (1.0, 2.0, 5.0, 10.0, 20.0, 50.0):
            if extra > base_tol:
                tol_schedule.append(extra)

        # Try shapely first for best quality, then pure-Python RDP, then decimation.
        try:
            from shapely.geometry import Polygon
            coords_2d = [(p["x"], p["z"]) for p in pts] + [(pts[0]["x"], pts[0]["z"])]
            poly = Polygon(coords_2d)
            if not poly.is_valid:
                poly = poly.buffer(0)
            for tol in tol_schedule:
                s = poly.simplify(tol, preserve_topology=True)
                if s.is_empty or not hasattr(s, "exterior") or s.exterior is None:
                    continue
                s_coords = list(s.exterior.coords)[:-1]  # drop closing dup
                if 3 <= len(s_coords) <= max_pts:
                    result = []
                    for cx, cz in s_coords:
                        nearest = min(pts, key=lambda p, cx=cx, cz=cz: (p["x"] - cx) ** 2 + (p["z"] - cz) ** 2)
                        result.append({"x": cx, "y": nearest["y"], "z": cz})
                    return result
        except Exception:
            pass

        # Pure-Python RDP with escalating tolerance
        for tol in tol_schedule:
            closed = pts + [pts[0]]
            s = _rdp(closed, tol)
            if len(s) >= 2 and s[-1]["x"] == s[0]["x"] and s[-1]["z"] == s[0]["z"]:
                s = s[:-1]
            if 3 <= len(s) <= max_pts:
                return s

        # Last resort: uniform decimation
        step = max(1, math.ceil(len(pts) / max_pts))
        return pts[::step][:max_pts]

    @staticmethod
    def _simplify_local_polyline(
        pts: list[dict],
        max_pts: int = MAX_SPLINE_POINTS_NATURAL,
    ) -> list[dict]:
        """
        Reduce a local-coordinate open polyline to at most *max_pts* points
        using Ramer-Douglas-Peucker in the XZ plane, falling back to uniform
        decimation if shapely is unavailable or all tolerances fail.

        Both endpoints are always preserved (unlike :py:meth:`_simplify_local_ring`,
        which closes the loop). *pts* is a list of {"x":…, "y":…, "z":…} dicts
        in local metres.

        v1.4.4 — simplification ALWAYS runs (previously bypassed for any line
        ≤200 pts). Tolerance is :func:`adaptive_tolerance` scaled to the bbox
        diagonal; shapely simplify uses ``preserve_topology=True`` to avoid
        the self-intersecting "spiral" output reported in #93.
        """
        if len(pts) < 3:
            return pts

        def _rdp(points, tol):
            if len(points) <= 2:
                return list(points)
            ax, az = points[0]["x"], points[0]["z"]
            bx, bz = points[-1]["x"], points[-1]["z"]
            dx, dz = bx - ax, bz - az
            seg_len_sq = dx * dx + dz * dz

            def _perp(p):
                if seg_len_sq == 0:
                    return math.hypot(p["x"] - ax, p["z"] - az)
                t = max(0.0, min(1.0, ((p["x"] - ax) * dx + (p["z"] - az) * dz) / seg_len_sq))
                return math.hypot(p["x"] - (ax + t * dx), p["z"] - (az + t * dz))

            max_d, idx = 0.0, 0
            for i in range(1, len(points) - 1):
                d = _perp(points[i])
                if d > max_d:
                    max_d, idx = d, i
            if max_d > tol:
                return _rdp(points[:idx + 1], tol)[:-1] + _rdp(points[idx:], tol)
            return [points[0], points[-1]]

        base_tol = adaptive_tolerance(pts)
        tol_schedule = [base_tol]
        for extra in (1.0, 2.0, 5.0, 10.0, 20.0):
            if extra > base_tol:
                tol_schedule.append(extra)

        # Try shapely first for best quality, then pure-Python RDP, then decimation.
        try:
            from shapely.geometry import LineString
            coords_2d = [(p["x"], p["z"]) for p in pts]
            line = LineString(coords_2d)
            for tol in tol_schedule:
                s = line.simplify(tol, preserve_topology=True)
                if s.is_empty:
                    continue
                s_coords = list(s.coords)
                if 2 <= len(s_coords) <= max_pts:
                    result = []
                    for cx, cz in s_coords:
                        nearest = min(pts, key=lambda p, cx=cx, cz=cz: (p["x"] - cx) ** 2 + (p["z"] - cz) ** 2)
                        result.append({"x": cx, "y": nearest["y"], "z": cz})
                    # Always preserve true endpoints (snap-to-nearest could
                    # have drifted the first/last vertex slightly).
                    if result:
                        result[0] = {"x": pts[0]["x"], "y": pts[0]["y"], "z": pts[0]["z"]}
                        result[-1] = {"x": pts[-1]["x"], "y": pts[-1]["y"], "z": pts[-1]["z"]}
                    return result
        except Exception:
            pass

        # Pure-Python RDP with escalating tolerance — open polyline, do not close.
        for tol in tol_schedule:
            s = _rdp(pts, tol)
            if 2 <= len(s) <= max_pts:
                return s

        # Last resort: uniform decimation, but always keep first + last.
        step = max(1, math.ceil(len(pts) / max_pts))
        decimated = pts[::step][:max_pts]
        if decimated and decimated[-1] is not pts[-1]:
            if len(decimated) >= max_pts:
                decimated[-1] = pts[-1]
            else:
                decimated.append(pts[-1])
        return decimated

    def _closed_spline_entity_from_ring(
        self,
        ring: list[list[float]],
        entity_prefix: str,
        index: int,
        comment_suffix: str = "",
        child_prefab: Optional[str] = None,
        naming_kind: Optional[str] = None,
        feature_properties: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Project a single GeoJSON ring (list of [lon, lat] pairs) to a closed
        SplineShapeEntity. Returns None if the ring has fewer than 3 in-bounds
        points after clipping to the terrain.

        ``comment_suffix`` is appended to the entity declaration line as a
        trailing ``// ...`` comment so callers can label the spline.

        ``child_prefab``: when not None, a generator child block is appended
        inside the entity using the ``${guid}path.et { coords 0 0 0 }`` syntax.

        TODO: this nested-child syntax inside a SplineShapeEntity body is
        unverified — the same form on roads (v1.1.0) caused Workbench to hang
        at 4% on world load and was reverted in v1.2.3. The Phase 3 forest /
        lake catalogues (``KNOWN_FOREST_PREFABS`` / ``KNOWN_LAKE_PREFABS``)
        ship empty so this path stays dormant; populating either catalogue
        may re-arm the same hang. Verify in Workbench before merging entries.
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

        in_bounds = self._simplify_local_ring(in_bounds)

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

        # Pick a descriptive entity name when the caller has supplied a
        # naming_kind. Otherwise fall back to the legacy sequential ID
        # so callers that haven't migrated still get a valid file.
        comment_extra = comment_suffix or ""
        if naming_kind and self._namer is not None:
            entity_name = self._namer.make_name(
                naming_kind,
                properties=feature_properties or {},
                x_local=origin["x"],
                z_local=origin["z"],
            )
            paints = expected_surface(naming_kind, feature_properties or {})
            if paints:
                self._record_surface(entity_name, paints)
                paints_token = f" // paints: {paints}"
                comment_extra = (
                    comment_extra + paints_token if comment_extra
                    else paints_token
                )
        else:
            entity_name = f"{entity_prefix}_{index}"

        return (
            f'SplineShapeEntity {entity_name} {{{comment_extra}\n'
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
 World "{{{self.world_ent_guid}}}Worlds/{self.map_name}.ent"
 m_sName "{self.map_name}"
 m_sGameMode "GameMaster"
 m_iPlayerCount 64
}}
'''
