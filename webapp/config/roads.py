"""Road classification tables for Enfusion (Atlas 2 alignment, v1.4.0)."""

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

# ---------------------------------------------------------------------------
# Atlas 2 canonical Enfusion road prefab catalogue
# ---------------------------------------------------------------------------
# Names sourced directly from the SCR_SHPPrefabDataList section of
# "The Atlas 2: Arma Reforger Terrain Creation Guide" (Jakerod).
# Located in stock Reforger at:
#   {58D0FB3206B6F859}Prefabs/WEGenerators/Roads/
#
# v1.4.0 replaces our earlier fabricated `RG_Road_<Surface>_<Width>m` scheme
# (e.g. `RG_Road_Asphalt_8m`) which only sometimes matched the shipped names.
# Every entry below is a name documented in Atlas 2 — width and lane count
# are baked into the prefab variant, not the filename suffix.
KNOWN_ROAD_PREFABS: frozenset[str] = frozenset({
    # Asphalt family (Everon-set, "E_<NN>" suffix)
    "RG_Road_Asphalt_E_01",            # standard 2-lane
    "RG_Road_Asphalt_E_01_DashedLine", # standard 2-lane with dashed centre line
    "RG_Road_Asphalt_E_01_Narrow",     # narrow asphalt (service / small streets)
    "RG_Road_Asphalt_E_02",            # wider asphalt (primary / secondary)
    "RG_Road_Asphalt_E_03",            # widest asphalt (motorway / trunk)
    # Cobblestone
    "RG_Road_Cobblestone_01",
    # Dirt
    "RG_Road_Dirt_01",
    "RG_Road_Dirt_02",
    "RG_Road_Forest_01",               # forest service road
    # Trails (foot / animal / atv)
    "RG_TrailDirt_01",
    "RG_TrailGravel_01",
})

# Default surface inferred from each prefab name (for fallback selection).
PREFAB_SURFACE: dict[str, str] = {
    "RG_Road_Asphalt_E_01": "asphalt",
    "RG_Road_Asphalt_E_01_DashedLine": "asphalt",
    "RG_Road_Asphalt_E_01_Narrow": "asphalt",
    "RG_Road_Asphalt_E_02": "asphalt",
    "RG_Road_Asphalt_E_03": "asphalt",
    "RG_Road_Cobblestone_01": "cobblestone",
    "RG_Road_Dirt_01": "dirt",
    "RG_Road_Dirt_02": "dirt",
    "RG_Road_Forest_01": "gravel",
    "RG_TrailDirt_01": "dirt",
    "RG_TrailGravel_01": "gravel",
}

# Single source of truth for OSM highway → Enfusion road mapping.
# Each row picks the closest Atlas 2 prefab for the OSM tag's typical
# width and surface.
OSM_ROAD_TAGS: dict[str, dict] = {
    # --- Motorway / trunk: widest asphalt ---
    "motorway":      {"width": 14,  "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_03"},
    "motorway_link": {"width": 8,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_02"},
    "trunk":         {"width": 10,  "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_02"},
    "trunk_link":    {"width": 7,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_02"},
    # --- Primary / secondary: standard 2-lane asphalt ---
    "primary":       {"width": 8,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_02"},
    "primary_link":  {"width": 6,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01"},
    "secondary":     {"width": 7,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01"},
    "secondary_link": {"width": 5,  "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_Narrow"},
    "tertiary":      {"width": 6,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01"},
    "tertiary_link": {"width": 5,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_Narrow"},
    # --- Residential / small urban: dashed-line variant ---
    "residential":   {"width": 5,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_DashedLine"},
    "unclassified":  {"width": 4,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_DashedLine"},
    # --- Service / narrow: narrow asphalt ---
    "service":       {"width": 3.5, "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_Narrow"},
    "living_street": {"width": 4,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_Narrow"},
    "cycleway":      {"width": 2,   "surface": "asphalt",     "enfusion_prefab": "RG_Road_Asphalt_E_01_Narrow"},
    # --- Tracks: gravel forest road by default ---
    "track":         {"width": 3,   "surface": "gravel",      "enfusion_prefab": "RG_TrailGravel_01"},
    # --- Trails: dirt footpath ---
    "path":          {"width": 1.5, "surface": "dirt",        "enfusion_prefab": "RG_TrailDirt_01"},
    "footway":       {"width": 1.5, "surface": "dirt",        "enfusion_prefab": "RG_TrailDirt_01"},
    "bridleway":     {"width": 2,   "surface": "dirt",        "enfusion_prefab": "RG_TrailDirt_01"},
}

# Derived dicts for backward compatibility with road_processor.
ROAD_DEFAULT_WIDTH: dict[str, dict[str, float]] = {
    k: {"width": v["width"]} for k, v in OSM_ROAD_TAGS.items()
}

ROAD_ENFUSION_PREFAB: dict[str, str] = {
    k: v["enfusion_prefab"] for k, v in OSM_ROAD_TAGS.items()
}

# (surface, width_class) → Atlas 2 prefab. Used by road_processor when
# OSM_ROAD_TAGS doesn't directly cover the highway type.
ROAD_PREFAB_BY_CLASS: dict[tuple[str, str], str] = {
    ("asphalt", "wide"):    "RG_Road_Asphalt_E_03",
    ("asphalt", "medium"):  "RG_Road_Asphalt_E_01",
    ("asphalt", "narrow"):  "RG_Road_Asphalt_E_01_Narrow",
    ("gravel", "wide"):     "RG_Road_Forest_01",
    ("gravel", "medium"):   "RG_TrailGravel_01",
    ("gravel", "narrow"):   "RG_TrailGravel_01",
    ("dirt", "wide"):       "RG_Road_Dirt_02",
    ("dirt", "medium"):     "RG_Road_Dirt_01",
    ("dirt", "narrow"):     "RG_TrailDirt_01",
    ("cobblestone", "wide"):   "RG_Road_Cobblestone_01",
    ("cobblestone", "medium"): "RG_Road_Cobblestone_01",
    ("cobblestone", "narrow"): "RG_Road_Cobblestone_01",
}


def _surface_from_legacy_or_canonical(name: str) -> str | None:
    """
    Best-effort surface inference for any RG_*-style road prefab name.

    Handles:
    - Atlas 2 canonical names (Asphalt_E_01, Dirt_02, Forest_01, ...).
    - Legacy fabricated names from v1.3.x (Asphalt_8m, Gravel_4m, ...).
    """
    if name in PREFAB_SURFACE:
        return PREFAB_SURFACE[name]

    # Legacy `RG_Road_<Surface>_<W>m` pattern from v1.3.x — preserve so that
    # validate_road_prefab() can still find a sensible new home for old names
    # that may still live in metadata files or third-party catalogues.
    lower = name.lower()
    if lower.startswith("rg_road_") and lower.endswith("m"):
        body = name[len("RG_Road_"):-1]
        head = body.split("_", 1)[0].lower()
        if head in ("asphalt", "gravel", "dirt", "cobblestone"):
            return head
    if lower.startswith("rg_trail"):
        if "gravel" in lower:
            return "gravel"
        if "dirt" in lower:
            return "dirt"
    return None


def validate_road_prefab(name: str) -> str:
    """
    Return ``name`` if it matches a known Enfusion road prefab from the
    Atlas 2 catalogue, otherwise snap to the closest match on the same
    surface. Falls back to ``RG_Road_Asphalt_E_01_Narrow`` if surface
    inference fails — that variant is a safe, generic asphalt prefab.

    Prevents the road layer from emitting fabricated prefab names that
    don't exist in a stock Reforger install (Workbench silently drops the
    road generator at world load).
    """
    if name in KNOWN_ROAD_PREFABS:
        return name

    surface = _surface_from_legacy_or_canonical(name)
    if surface is None:
        return "RG_Road_Asphalt_E_01_Narrow"

    # Preference list per surface (widest match first, gracefully degrading).
    fallback_by_surface = {
        "asphalt": [
            "RG_Road_Asphalt_E_01",
            "RG_Road_Asphalt_E_01_DashedLine",
            "RG_Road_Asphalt_E_02",
            "RG_Road_Asphalt_E_03",
            "RG_Road_Asphalt_E_01_Narrow",
        ],
        "gravel": [
            "RG_Road_Forest_01",
            "RG_TrailGravel_01",
        ],
        "dirt": [
            "RG_Road_Dirt_01",
            "RG_Road_Dirt_02",
            "RG_TrailDirt_01",
        ],
        "cobblestone": [
            "RG_Road_Cobblestone_01",
        ],
    }

    for candidate in fallback_by_surface.get(surface, []):
        if candidate in KNOWN_ROAD_PREFABS:
            return candidate

    return "RG_Road_Asphalt_E_01_Narrow"
