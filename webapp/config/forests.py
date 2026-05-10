"""
Forest classification + Enfusion Forest Generator prefab catalog.

Same architecture as config.buildings: catalog ships empty so we never
fabricate paths that fail to resolve in Workbench. Populate
KNOWN_FOREST_PREFABS with confirmed FG_*.et paths from a stock Reforger
install and the vegetation layer will auto-attach the generator child to
every matching forest spline on the next generation.
"""

from __future__ import annotations

# forest_type → Enfusion FG_*.et path. Empty by default.
# Keys are produced by forest_type_from_osm() from raw OSM feature properties.
# Valid keys: "coniferous", "deciduous", "mixed", "scrub", "heath"
#
# Example (confirm paths against your Reforger install before committing):
#   "coniferous": "Prefabs/WEGenerators/Forest/FG_PineForest_01.et",
#   "deciduous":  "Prefabs/WEGenerators/Forest/FG_DeciduousForest_01.et",
#   "mixed":      "Prefabs/WEGenerators/Forest/FG_MixedForest_01.et",
KNOWN_FOREST_PREFABS: dict[str, str] = {}


def validate_forest_prefab(forest_type: str | None) -> str | None:
    """
    Return the verified FG_*.et path for a forest type, or None if not cataloged.
    None signals the vegetation-layer emitter to fall back to spline-only mode.
    """
    if not forest_type:
        return None
    return KNOWN_FOREST_PREFABS.get(forest_type)


def forest_type_from_osm(props: dict) -> str:
    """
    Map raw OSM feature properties to a forest_type catalog key.

    Mirrors the classification logic in feature_extractor.extract_forest_features
    so the vegetation-layer emitter and the feature extractor agree on type names.
    """
    leaf_type = props.get("leaf_type", "")
    area_type = props.get("type", "")
    if leaf_type == "needleleaved":
        return "coniferous"
    if leaf_type == "broadleaved":
        return "deciduous"
    if leaf_type == "mixed":
        return "mixed"
    if area_type == "scrub":
        return "scrub"
    if area_type == "heath":
        return "heath"
    return "mixed"
