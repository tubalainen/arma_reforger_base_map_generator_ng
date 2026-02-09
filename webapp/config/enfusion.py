"""
Enfusion Workbench project generation constants.

Centralized configuration for generating Enfusion-compatible project files,
world entities, surface material mappings, and prefab paths.

All paths and GUIDs are verified against the official Bohemia Interactive
Community Wiki (community.bistudio.com) as of 2025-02-09.
"""

# ---------------------------------------------------------------------------
# Base game dependency
# ---------------------------------------------------------------------------

ARMA_REFORGER_GUID = "58D0FB3206B6F859"

# ---------------------------------------------------------------------------
# Valid Enfusion terrain dimensions
# ---------------------------------------------------------------------------

# Enfusion terrain uses faces (power of 2). Heightmap vertex count = faces + 1.
VALID_ENFUSION_FACE_COUNTS = [128, 256, 512, 1024, 2048, 4096, 8192]
VALID_ENFUSION_VERTEX_COUNTS = [f + 1 for f in VALID_ENFUSION_FACE_COUNTS]
# => [129, 257, 513, 1025, 2049, 4097, 8193]

# ---------------------------------------------------------------------------
# Default world entity settings
# ---------------------------------------------------------------------------

WORLD_ENTITY_DEFAULTS = {
    "sky_preset": "Atmosphere.emat",
    "planet_presets": [
        "Stars_01.emat",
        "Sun_01.emat",
        "Moon_01.emat",
        "Clouds_Distant.emat",
    ],
    "clouds_preset": "Clouds_Volumetric.emat",
    "ocean_material": "ocean.emat",
    "ocean_simulation": "oceanSimIsland.emat",
    "lens_flares": "LensFlares.conf",
}

# ---------------------------------------------------------------------------
# Terrain LOD defaults
# ---------------------------------------------------------------------------

TERRAIN_LOD_DEFAULTS = {
    "close_distance_max": 200,
    "close_distance_blend": 200,
    "middle_distance_max": 750,
    "middle_distance_blend": 400,
    "layer_preset": "Terrain",
}

# ---------------------------------------------------------------------------
# Surface material mapping
# ---------------------------------------------------------------------------

# Base path for all Enfusion surface materials
SURFACE_MATERIAL_BASE = "ArmaReforger/Terrains/Common/Surfaces"

# Map from our generated mask name -> Enfusion material resource path
SURFACE_MATERIAL_MAP = {
    "grass": f"{SURFACE_MATERIAL_BASE}/Grass_01.emat",
    "forest_floor": f"{SURFACE_MATERIAL_BASE}/ForestFloor_01.emat",
    "asphalt": f"{SURFACE_MATERIAL_BASE}/Asphalt_01.emat",
    "rock": f"{SURFACE_MATERIAL_BASE}/Rock_01.emat",
    "sand_dirt": f"{SURFACE_MATERIAL_BASE}/Dirt_01.emat",
}

# Alternative materials the user can swap to (for SETUP_GUIDE reference)
SURFACE_MATERIAL_ALTERNATIVES = {
    "grass": [
        f"{SURFACE_MATERIAL_BASE}/Grass_02.emat",
        f"{SURFACE_MATERIAL_BASE}/Grass_03.emat",
    ],
    "forest_floor": [
        f"{SURFACE_MATERIAL_BASE}/ForestFloor_Pine_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Moss_01.emat",
    ],
    "asphalt": [
        f"{SURFACE_MATERIAL_BASE}/Concrete_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Asphalt_Cracked_01.emat",
    ],
    "rock": [
        f"{SURFACE_MATERIAL_BASE}/Rock_Granite_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Gravel_01.emat",
    ],
    "sand_dirt": [
        f"{SURFACE_MATERIAL_BASE}/Sand_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Mud_01.emat",
    ],
}

# Recommended import order (most specific -> least specific)
SURFACE_IMPORT_ORDER = ["rock", "forest_floor", "asphalt", "sand_dirt"]

# ---------------------------------------------------------------------------
# Required world prefabs (verified from BI wiki "Suggested Default Prefabs")
# ---------------------------------------------------------------------------

WORLD_PREFABS = {
    "terrain": "Prefabs/World/Game/GenericTerrain_Default.et",
    "lighting": "Prefabs/World/Lighting/Lighting_Default.et",
    "fog": "Prefabs/World/Game/FogHaze_Default.et",
    "post_processing": "Prefabs/World/Game/GenericWorldPP_Default.et",
    "env_probe": "Prefabs/World/Lighting/EnvProbe_Default.et",
    "camera": "Prefabs/World/Game/SCR_CameraManager.et",
    "time_weather": "Prefabs/World/Game/TimeAndWeatherManager.et",
    "projectile_sounds": "Prefabs/World/Game/ProjectileSoundsManager.et",
    "map_entity": "Prefabs/World/Game/MapEntity.et",
    "sound_world": "Prefabs/World/Game/SoundWorld_Base.et",
    "forest_sync": "Prefabs/World/Game/ForestSyncManager.et",
    "destruction": "Prefabs/World/Game/DestructionManager.et",
    "gamemode_editor": "Prefabs/MP/Modes/GameMaster/GameMode_Editor_Full.et",
}

# ---------------------------------------------------------------------------
# Generator prefab base paths (verified from wiki Directory Structure)
# ---------------------------------------------------------------------------

ROAD_PREFAB_BASE = "Prefabs/WEGenerators/Roads"
FOREST_PREFAB_BASE = "Prefabs/WEGenerators/Forest"
LAKE_PREFAB_BASE = "Prefabs/WEGenerators/Water/Lake"

# ---------------------------------------------------------------------------
# Project file generation
# ---------------------------------------------------------------------------

# Default modding directory (verified from wiki "Mod Project Setup")
DEFAULT_ADDON_DIR = r"%userProfile%\Documents\My Games\ArmaReforgerWorkbench\addons"

# Platform configurations included in addon.gproj
PLATFORM_CONFIGS = ["PC", "XBOX_ONE", "XBOX_SERIES", "PS4", "PS5", "HEADLESS"]

# Resource class configurations included in .meta files
RESOURCE_CLASS_CONFIGS = {
    "ent": "ENTResourceClass",
    "conf": "CONFResourceClass",
    "layer": "LayerResourceClass",
}

# Characters allowed in Enfusion project names
PROJECT_NAME_ALLOWED_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_- ."
)

# Maximum project name length
PROJECT_NAME_MAX_LENGTH = 64

# ---------------------------------------------------------------------------
# Enfusion block system constants
# ---------------------------------------------------------------------------

# One block = 32x32 faces at highest LOD (33x33 vertices)
BLOCK_FACE_SIZE = 32
BLOCK_VERTEX_SIZE = BLOCK_FACE_SIZE + 1  # 33

# Maximum surfaces per block
MAX_SURFACES_PER_BLOCK = 5

# Recommended max externally-generated masks (leave room for manual refinement)
RECOMMENDED_MAX_EXTERNAL_MASKS = 3

# Surface mask pixel threshold for "meaningful coverage" in block analysis
BLOCK_SURFACE_THRESHOLD = 10  # out of 255


def snap_to_enfusion_size(requested_size: int) -> int:
    """
    Snap a requested heightmap dimension to the nearest valid Enfusion vertex count.

    Enfusion terrain uses faces that must be a power of 2.
    Heightmap = faces + 1 vertices. Valid sizes: 129, 257, 513, 1025, 2049, 4097, 8193.

    Args:
        requested_size: The user's requested heightmap dimension in pixels.

    Returns:
        The nearest valid Enfusion heightmap vertex count.
    """
    return min(VALID_ENFUSION_VERTEX_COUNTS, key=lambda x: abs(x - requested_size))


def compute_height_scale(min_elevation: float, max_elevation: float) -> float:
    """
    Compute the Enfusion height scale for a given elevation range.

    Height scale maps the 16-bit heightmap range (0-65535) to real-world metres.
    Formula: height_scale = elevation_range / 65535

    Args:
        min_elevation: Minimum elevation in metres.
        max_elevation: Maximum elevation in metres.

    Returns:
        Height scale value for Enfusion terrain entity.
    """
    elev_range = max(max_elevation - min_elevation, 0.01)
    return elev_range / 65535.0


def compute_terrain_size(face_count: int, cell_size: float) -> float:
    """
    Compute total terrain size in metres.

    Args:
        face_count: Number of terrain faces (power of 2).
        cell_size: Grid cell size in metres.

    Returns:
        Total terrain dimension in metres.
    """
    return face_count * cell_size
