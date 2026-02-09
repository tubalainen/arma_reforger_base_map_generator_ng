"""Road classification tables for Enfusion."""

# Country-specific road surface inference rules
ROAD_DEFAULT_SURFACE: dict[str, dict[str, str]] = {
    "NO": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "gravel",
        "forest_road_surface": "gravel",
    },
    "SE": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "asphalt",
        "forest_road_surface": "gravel",
    },
    "FI": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "gravel",
        "forest_road_surface": "gravel",
    },
    "DK": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "asphalt",
        "forest_road_surface": "gravel",
    },
    "EE": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "gravel",
        "forest_road_surface": "dirt",
    },
    "LV": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "gravel",
        "forest_road_surface": "dirt",
    },
    "LT": {
        "track_surface_default": "gravel",
        "residential_rural_surface": "asphalt",
        "forest_road_surface": "gravel",
    },
}

# Single source of truth for road type configuration.
# Each highway type has default width, surface, and Enfusion prefab.
#
# Prefab names follow the pattern: RG_Road_{Surface}_{Width}m
# Located at: {58D0FB3206B6F859}Prefabs/WEGenerators/Roads/
# NOTE: These prefab names are best-effort guesses based on the naming
# convention. If a prefab doesn't exist in your Arma Reforger version,
# check the Resource Browser (search "RG_") for available road prefabs
# and update the names here.
OSM_ROAD_TAGS: dict[str, dict] = {
    "motorway":      {"width": 14,  "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_14m"},
    "motorway_link": {"width": 8,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_8m"},
    "trunk":         {"width": 10,  "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_10m"},
    "trunk_link":    {"width": 7,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_8m"},
    "primary":       {"width": 8,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_8m"},
    "primary_link":  {"width": 6,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_6m"},
    "secondary":     {"width": 7,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_7m"},
    "secondary_link": {"width": 5,  "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_5m"},
    "tertiary":      {"width": 6,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_6m"},
    "tertiary_link": {"width": 5,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_5m"},
    "residential":   {"width": 5,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_5m"},
    "unclassified":  {"width": 4,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_4m"},
    "service":       {"width": 3.5, "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_4m"},
    "track":         {"width": 3,   "surface": "gravel",  "enfusion_prefab": "RG_Road_Gravel_4m"},
    "living_street": {"width": 4,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_4m"},
    "path":          {"width": 1.5, "surface": "dirt",    "enfusion_prefab": "RG_Road_Dirt_2m"},
    "footway":       {"width": 1.5, "surface": "dirt",    "enfusion_prefab": "RG_Road_Dirt_2m"},
    "cycleway":      {"width": 2,   "surface": "asphalt", "enfusion_prefab": "RG_Road_Asphalt_2m"},
    "bridleway":     {"width": 2,   "surface": "dirt",    "enfusion_prefab": "RG_Road_Dirt_2m"},
}

# Derived dicts for backward compatibility
ROAD_DEFAULT_WIDTH: dict[str, dict[str, float]] = {
    k: {"width": v["width"]} for k, v in OSM_ROAD_TAGS.items()
}

ROAD_ENFUSION_PREFAB: dict[str, str] = {
    k: v["enfusion_prefab"] for k, v in OSM_ROAD_TAGS.items()
}
