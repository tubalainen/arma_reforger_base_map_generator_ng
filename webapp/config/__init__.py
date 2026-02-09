"""
Application configuration package.

Re-exports all configuration values from sub-modules so that existing
``from config import X`` statements continue to work unchanged.

Configuration is split into focused modules:
- paths: BASE_DIR, OUTPUT_DIR, HOST, PORT
- countries: COUNTRY_BOUNDS, COUNTRY_CRS, COUNTRY_NAMES, TREELINE_ELEVATION
- elevation: CountryElevationConfig, ELEVATION_CONFIGS, EU_DEM_CONFIG, API keys
- roads: ROAD_DEFAULT_SURFACE, OSM_ROAD_TAGS, ROAD_DEFAULT_WIDTH, ROAD_ENFUSION_PREFAB
- surfaces: SURFACE_CLASSES
- endpoints: OVERPASS_*, OPENTOPOGRAPHY_*, SENTINEL2_*, CORINE_*, TREE_COVER_*
- terrain: MAX_TERRAIN_SIZE, DEFAULT_GRID_CELL_SIZE, height scale defaults
"""

# Paths & server
from config.paths import BASE_DIR, OUTPUT_DIR, HOST, PORT

# Country data
from config.countries import (
    COUNTRY_BOUNDS, COUNTRY_CRS, COUNTRY_NAMES, TREELINE_ELEVATION,
)

# Elevation APIs
from config.elevation import (
    CountryElevationConfig, ELEVATION_CONFIGS, EU_DEM_CONFIG,
    OPENTOPOGRAPHY_API_KEY,
    DATAFORSYNINGEN_TOKEN, NLS_FINLAND_API_KEY,
)

# Lantmäteriet (Sweden)
from config.lantmateriet import LantmaterietConfig, LANTMATERIET_CONFIG

# Road classification
from config.roads import (
    ROAD_DEFAULT_SURFACE, OSM_ROAD_TAGS,
    ROAD_DEFAULT_WIDTH, ROAD_ENFUSION_PREFAB,
)

# Surface classes
from config.surfaces import SURFACE_CLASSES

# External API endpoints
from config.endpoints import (
    OVERPASS_ENDPOINTS, OVERPASS_ENDPOINT, OVERPASS_FALLBACK_ENDPOINT, OVERPASS_TIMEOUT,
    OPENTOPOGRAPHY_ENDPOINT,
    SENTINEL2_WMS_ENDPOINT, SENTINEL2_WMTS_URL,
    CORINE_WMS, TREE_COVER_REST,
)

# Terrain defaults
from config.terrain import (
    MAX_MAP_EXTENT_M, MAX_TERRAIN_SIZE, DEFAULT_GRID_CELL_SIZE,
    DEFAULT_HEIGHT_SCALE, ENFUSION_HEIGHT_SCALE_DEFAULT,
    ENFUSION_MAX_SURFACES_PER_BLOCK, DEFAULT_TARGET_CRS,
)

# Enfusion Workbench project generation
from config.enfusion import (
    ARMA_REFORGER_GUID,
    VALID_ENFUSION_VERTEX_COUNTS, VALID_ENFUSION_FACE_COUNTS,
    WORLD_ENTITY_DEFAULTS, TERRAIN_LOD_DEFAULTS,
    SURFACE_MATERIAL_MAP, SURFACE_MATERIAL_ALTERNATIVES,
    SURFACE_MATERIAL_BASE, SURFACE_IMPORT_ORDER,
    WORLD_PREFABS, ROAD_PREFAB_BASE, FOREST_PREFAB_BASE, LAKE_PREFAB_BASE,
    DEFAULT_ADDON_DIR, PLATFORM_CONFIGS, RESOURCE_CLASS_CONFIGS,
    PROJECT_NAME_ALLOWED_CHARS, PROJECT_NAME_MAX_LENGTH,
    BLOCK_FACE_SIZE, BLOCK_VERTEX_SIZE, MAX_SURFACES_PER_BLOCK,
    RECOMMENDED_MAX_EXTERNAL_MASKS, BLOCK_SURFACE_THRESHOLD,
    snap_to_enfusion_size, compute_height_scale, compute_terrain_size,
)
