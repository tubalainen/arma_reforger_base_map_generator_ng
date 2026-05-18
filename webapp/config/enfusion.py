"""
Enfusion Workbench project generation constants.

Centralized configuration for generating Enfusion-compatible project files,
world entities, surface material mappings, and prefab paths.

All paths and GUIDs are verified against the official Bohemia Interactive
Community Wiki (community.bistudio.com) as of 2025-02-09.
"""

# ---------------------------------------------------------------------------
# Generator version
# ---------------------------------------------------------------------------
# Single source of truth — imported by main.py for the web UI and by
# enfusion_project_generator.py to stamp into every generated file header.
# Bump here on every release; the README Docker tag pin should match.

APP_VERSION = "1.5.4"

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
# Default world entity settings — REMOVED in v1.5.0
# ---------------------------------------------------------------------------
# The pre-1.5 generator emitted a hand-built `GenericWorldEntity { SkyPreset
# { … } PlanetPresets { … } SkyVolCloudsRenderer { … } OceanPreset { … } }`
# block at the top of every `_default.layer`. Inspection of a Workbench-saved
# reference layer (Testprojekt/Worlds/testworld_Layers/default.layer) shows
# none of those classes exist in the current Reforger schema — the parser
# rejects each one as "Unknown class" and discards everything in the layer
# that followed. We now emit no environment block at all and let Workbench
# rebuild the `GenericWorldEntity world { BSP … boundMins … }` block on its
# own at first save. See issue #111.

# ---------------------------------------------------------------------------
# Terrain LOD defaults — REMOVED in v1.5.0
# ---------------------------------------------------------------------------
# The pre-1.5 generator wrote `CloseDistanceMax / CloseDistanceBlend /
# MiddleDistanceMax / MiddleDistanceBlend` (and `TerrainGridSizeX/Z`,
# `GridCellSize`, `HeightScale`, `HeightOffset`) as inline properties of
# `GenericTerrainEntity` in the layer file. The Workbench-saved reference
# shows that block is empty — those parameters live in the on-disk
# `tileMap.conf` Workbench writes when right-click → "Create new terrain"
# fires. Emitting them inline triggered the "Unknown keyword/data" cascade
# in Gunnar's error.log and was the proximate cause of the paint crash
# (issue #111).

# ---------------------------------------------------------------------------
# Surface material mapping
# ---------------------------------------------------------------------------

# Base path for all Enfusion surface materials
SURFACE_MATERIAL_BASE = "ArmaReforger/Terrains/Common/Surfaces"

# Map from our generated mask name -> Enfusion material resource path
SURFACE_MATERIAL_MAP = {
    "grass": f"{SURFACE_MATERIAL_BASE}/Grass_01.emat",
    "forest_floor": f"{SURFACE_MATERIAL_BASE}/ForestFloor_01.emat",
    "pine_floor": f"{SURFACE_MATERIAL_BASE}/ForestFloor_Pine_01.emat",
    "asphalt": f"{SURFACE_MATERIAL_BASE}/Asphalt_01.emat",
    "gravel": f"{SURFACE_MATERIAL_BASE}/Gravel_01.emat",
    "dirt": f"{SURFACE_MATERIAL_BASE}/Dirt_01.emat",
    "rock": f"{SURFACE_MATERIAL_BASE}/Rock_01.emat",
    "sand": f"{SURFACE_MATERIAL_BASE}/Sand_01.emat",
    "water_edge": f"{SURFACE_MATERIAL_BASE}/Mud_01.emat",
}

# Alternative materials the user can swap to (for SETUP_GUIDE reference)
SURFACE_MATERIAL_ALTERNATIVES = {
    "grass": [
        f"{SURFACE_MATERIAL_BASE}/Grass_02.emat",
        f"{SURFACE_MATERIAL_BASE}/Grass_03.emat",
    ],
    "forest_floor": [
        f"{SURFACE_MATERIAL_BASE}/Moss_01.emat",
    ],
    "pine_floor": [
        f"{SURFACE_MATERIAL_BASE}/ForestFloor_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Moss_01.emat",
    ],
    "asphalt": [
        f"{SURFACE_MATERIAL_BASE}/Concrete_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Asphalt_Cracked_01.emat",
    ],
    "gravel": [
        f"{SURFACE_MATERIAL_BASE}/Rock_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Dirt_01.emat",
    ],
    "dirt": [
        f"{SURFACE_MATERIAL_BASE}/Mud_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Sand_01.emat",
    ],
    "rock": [
        f"{SURFACE_MATERIAL_BASE}/Rock_Granite_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Gravel_01.emat",
    ],
    "sand": [
        f"{SURFACE_MATERIAL_BASE}/Dirt_01.emat",
    ],
    "water_edge": [
        f"{SURFACE_MATERIAL_BASE}/Sand_01.emat",
        f"{SURFACE_MATERIAL_BASE}/Dirt_01.emat",
    ],
}

# Recommended import order (most specific -> least specific)
SURFACE_IMPORT_ORDER = [
    "rock", "pine_floor", "forest_floor", "asphalt",
    "gravel", "dirt", "sand", "water_edge",
]

# ---------------------------------------------------------------------------
# Required world prefabs — paths/GUIDs/classes verified against a clean
# Workbench-saved reference at
#   …/ArmaReforgerWorkbench/addons/Testprojekt/Worlds/testworld_Layers/default.layer
# (one prefab per Atlas 2 §"Environment", produced by dragging each .et into
# the editor and saving). Every entry below appears verbatim in that file —
# anything we can't read off the reference is *not* shipped.
# ---------------------------------------------------------------------------

WORLD_PREFABS = {
    "terrain":          "Prefabs/World/DefaultWorld/GenericTerrain_Default.et",
    "lighting":         "Prefabs/World/DefaultWorld/Lighting_Default.et",
    "fog":              "Prefabs/World/DefaultWorld/FogHaze_Default.et",
    "post_processing":  "Prefabs/World/DefaultWorld/GenericWorldPP_Default.et",
    "env_probe":        "Prefabs/World/DefaultWorld/EnvProbe_Default.et",
    "camera":           "Prefabs/World/Game/SCR_CameraManager.et",
    "time_weather":     "Prefabs/World/Game/TimeAndWeatherManager.et",
    "preload":          "Prefabs/World/Game/PreloadManager.et",
    "music_manager":    "Prefabs/Sounds/Music/MusicManager_Base.et",
    "radio_broadcast":  "Prefabs/Systems/Radio/RadioBroadcastManager_Everon.et",
    "forest_sync":      "Prefabs/World/Game/ForestSyncManager.et",
    "destruction":      "Prefabs/Systems/Destruction/DestructionManager.et",
    "mp_destruction":   "Prefabs/MP/MPDestructionManager.et",
    "sound_world":      "Prefabs/Sounds/SoundWorld/SoundWorld_Base.et",
    # GameMaster sub-scene prefab — not in `_default.layer`, only referenced
    # when/if we generate a gamemode sub-scene. Left at its previous path;
    # not verified against the reference layer (the reference is a base
    # scene, not a sub-scene).
    "gamemode_editor":  "Prefabs/MP/Modes/GameMaster/GameMode_Editor_Full.et",
}

# Per-prefab resource GUID. Workbench's resource DB requires the exact
# 16-hex GUID assigned to each prefab when it was registered — using the
# addon-level ARMA_REFORGER_GUID for all of them (the pre-1.5 mistake) made
# Workbench emit "Wrong GUID/name for resource …" and silently drop the
# prefab, which left the terrain entity uninitialised and crashed the NVTT
# bake on first brush stroke (issue #111).
WORLD_PREFAB_GUIDS: dict[str, str] = {
    "terrain":          "221ABC927C672E4E",
    "lighting":         "5B2B348D9520F7C7",
    "fog":              "78D9BBF0F423FEB4",
    "post_processing":  "3AFFB0B0EC055284",
    "env_probe":        "B6B6A21399C5571B",
    "camera":           "33F9FD881E3700CC",
    "time_weather":     "A3BAF78F6F03315B",
    "preload":          "104F8505FD1871EF",
    "music_manager":    "359452CCDBDD03F6",
    "radio_broadcast":  "66B93BC296E2F977",
    "forest_sync":      "7699E66077068406",
    "destruction":      "E5B570B5F32A7BAE",
    "mp_destruction":   "9BB369F2803C6F71",
    "sound_world":      "FBE5065D0273E9E1",
}

# Class name to emit on the left of the inheritance `:` in the layer file.
# Some prefabs are instantiated by their concrete component class, others
# by generic wrappers — both forms appear verbatim in the reference layer.
WORLD_PREFAB_CLASS: dict[str, str] = {
    "terrain":          "GenericTerrainEntity",
    "lighting":         "GenericWorldLightEntity",
    "fog":              "GenericWorldFogEntity",
    "post_processing":  "GenericWorldPPEffect",
    "env_probe":        "GameEnvironmentProbeEntity",
    "camera":           "SCR_CameraManager",
    "time_weather":     "TimeAndWeatherManagerEntity",
    "preload":          "BasePreloadManager",
    "music_manager":    "MusicManager",
    "radio_broadcast":  "RadioBroadcastManager",
    "forest_sync":      "GenericEntity",
    "destruction":      "SCR_DestructionManager",
    "mp_destruction":   "SCR_MPDestructionManager",
    "sound_world":      "SoundWorld",
}

# Two prefabs in the reference layer carry an explicit instance name
# between the class and the `:` — i.e. `<class> <instance> : "{GUID}path"`.
# All others are anonymous singletons.
WORLD_PREFAB_INSTANCE_NAME: dict[str, str] = {
    "camera":   "SCR_CameraManager1",
    "preload":  "PreloadManager1",
}

# Country code → biome-specific AmbientSounds prefab. v1.5.0 routes every
# country to AmbientSounds_Arland because that is the only prefab whose
# GUID + path we have verified against a saved Workbench layer. The
# previous AmbientSounds_Everon entry was an unverified guess — see
# the v1.5.0 plan, Open question #1. Revisit when we have a captured
# Workbench reference line for Everon.
AMBIENT_SOUND_PREFABS: dict[str, str] = {
    "default":          "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "NO":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "SE":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "FI":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "DK":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "EE":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "LV":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
    "LT":               "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et",
}

# Per-prefab GUID for each AmbientSounds variant. Only Arland is verified
# (lifted from the reference layer line at line 34); the wrapper class for
# AmbientSounds is `GenericEntity`, identical to forest_sync, so we don't
# need a separate `AMBIENT_SOUND_CLASS` table.
AMBIENT_SOUND_PREFAB_GUIDS: dict[str, str] = {
    "Prefabs/Sounds/Environment/Arland/AmbientSounds_Arland.et": "56DA7A01AE88C7D6",
}

# Keys from WORLD_PREFABS that are emitted into the managers layer.
# Used by SETUP_GUIDE generator to render the "Bootstrap entities" status
# table and by tests to assert layer completeness. `projectile_sounds`
# and `map_entity` are dropped from the bootstrap set in v1.5.0 because
# they don't appear in the Workbench-saved reference layer (they belong
# in the GameMaster sub-scene, not the base scene).
MANDATORY_BOOTSTRAP_KEYS: tuple[str, ...] = (
    "camera",
    "time_weather",
    "sound_world",
    "forest_sync",
    "destruction",
    "mp_destruction",
    "preload",
    "radio_broadcast",
    "music_manager",
)


def resolve_ambient_prefab(country_codes: list[str] | None) -> str:
    """
    Pick the biome-appropriate AmbientSounds_*.et prefab for a given set of
    country codes detected for the terrain. First matching country wins.
    Falls back to the "default" entry (AmbientSounds_Arland in v1.5.0).
    """
    if country_codes:
        for code in country_codes:
            if code in AMBIENT_SOUND_PREFABS:
                return AMBIENT_SOUND_PREFABS[code]
    return AMBIENT_SOUND_PREFABS["default"]

# ---------------------------------------------------------------------------
# Generator prefab base paths (verified from wiki Directory Structure)
# ---------------------------------------------------------------------------

# Atlas 2 (Jakerod) cross-reference (docs/Atlas2.pdf, p. 12 — the
# SCR_SHPPrefabDataList block) documents the canonical paths:
#   PrefabLibrary/Generators/Roads/<Asphalt|Cobblestone|Dirt>/<prefab>.et
# The legacy `Prefabs/WEGenerators/Roads/` path used in v1.3.x and earlier
# was a guess; Atlas 2's PDF (committed to the repo at docs/Atlas2.pdf)
# is the source of truth.
ROAD_PREFAB_BASE = "PrefabLibrary/Generators/Roads"
# Per-surface subdirectories — used when the SETUP_GUIDE quotes a
# fully-qualified path for the editor user to drag.
ROAD_PREFAB_SUBDIRS: dict[str, str] = {
    "asphalt": "Asphalt",
    "cobblestone": "Cobblestone",
    "dirt": "Dirt",       # also hosts gravel trail prefabs
    "gravel": "Dirt",     # RG_TrailGravel_01 / RG_Road_Forest_01 live here
}
# Forest / Lake generator base paths remain unverified against Atlas 2
# (the doc doesn't list them in a SCR_*PrefabDataList form). The
# KNOWN_FOREST_PREFABS / KNOWN_LAKE_PREFABS catalogues ship empty so we
# never fabricate unverified paths.
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

# Maximum ShapePoints per SplineShapeEntity.
# The Enfusion World Editor renderer freezes on very complex polygon splines
# (e.g. raw OSM forest boundaries can exceed 1 500 vertices).  Any ring that
# exceeds this limit is simplified with Ramer-Douglas-Peucker before emission.
MAX_SPLINE_POINTS = 200

# Tighter cap for "natural" splines (forests, lakes, rivers, wetlands).
# Atlas 2's manual workflow shows humans hand-clicking ~5–20 vertices for
# typical features — over-detailed splines look unnatural and make the World
# Editor sluggish.  Roads keep the looser MAX_SPLINE_POINTS because their
# shapes are dictated by real-world geometry and tested at that cap.
MAX_SPLINE_POINTS_NATURAL = 120

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


def snap_to_enfusion_dimensions(size_x: int, size_z: int) -> tuple[int, int]:
    """
    Snap X and Z vertex counts independently to valid Enfusion sizes.

    Enfusion supports non-square terrain — ``TerrainGridSizeX`` and
    ``TerrainGridSizeZ`` can differ, but each must be a power-of-2 face
    count (i.e. vertex count = 2^n + 1).

    Args:
        size_x: Requested vertex count along the X (width) axis.
        size_z: Requested vertex count along the Z (depth) axis.

    Returns:
        Tuple of (snapped_x, snapped_z) valid Enfusion vertex counts.
    """
    return (snap_to_enfusion_size(size_x), snap_to_enfusion_size(size_z))


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
