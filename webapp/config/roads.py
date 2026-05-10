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

# ---------------------------------------------------------------------------
# Known-good Enfusion road generator prefabs
# ---------------------------------------------------------------------------
# When the auto-inferred prefab name doesn't match any known prefab we fall
# back to the closest match by (surface, width) instead of writing a name we
# fabricated — fabricated names produce broken roads that fail to load in
# Workbench. The set below is the union of every prefab referenced from
# OSM_ROAD_TAGS plus the (surface, width_class) lookup table in
# services/road_processor.py.
KNOWN_ROAD_PREFABS: frozenset[str] = frozenset({
    # Asphalt
    "RG_Road_Asphalt_2m",
    "RG_Road_Asphalt_4m",
    "RG_Road_Asphalt_5m",
    "RG_Road_Asphalt_6m",
    "RG_Road_Asphalt_7m",
    "RG_Road_Asphalt_8m",
    "RG_Road_Asphalt_10m",
    "RG_Road_Asphalt_14m",
    # Gravel
    "RG_Road_Gravel_3m",
    "RG_Road_Gravel_4m",
    "RG_Road_Gravel_6m",
    # Dirt
    "RG_Road_Dirt_2m",
    "RG_Road_Dirt_3m",
    "RG_Road_Dirt_4m",
})


def _parse_prefab_name(name: str) -> tuple[str, float] | None:
    """Extract (surface, width_m) from an RG_Road_<Surface>_<W>m name."""
    if not name.startswith("RG_Road_") or not name.endswith("m"):
        return None
    body = name[len("RG_Road_"):-1]  # e.g. "Asphalt_6"
    parts = body.rsplit("_", 1)
    if len(parts) != 2:
        return None
    surface, width_str = parts
    try:
        return surface.lower(), float(width_str)
    except ValueError:
        return None


def validate_road_prefab(name: str) -> str:
    """
    Return ``name`` if it matches a known Enfusion road prefab, otherwise
    return the nearest known prefab on the same surface (or the first known
    asphalt prefab as a last resort).

    This prevents the road generator from emitting prefab paths that don't
    exist in a stock Reforger install — those would cause Workbench to
    silently drop the road generator at world load.
    """
    if name in KNOWN_ROAD_PREFABS:
        return name

    parsed = _parse_prefab_name(name)
    if parsed is None:
        return "RG_Road_Asphalt_4m"

    surface, width = parsed
    same_surface: list[tuple[float, str]] = []
    for known in KNOWN_ROAD_PREFABS:
        kp = _parse_prefab_name(known)
        if kp and kp[0] == surface:
            same_surface.append((kp[1], known))

    if not same_surface:
        return "RG_Road_Asphalt_4m"

    same_surface.sort(key=lambda x: abs(x[0] - width))
    return same_surface[0][1]
