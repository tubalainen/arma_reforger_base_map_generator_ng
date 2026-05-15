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
# Atlas 2 (docs/Atlas2.pdf, p. 19 — "Import Shapes with Forest Generators")
# names two specific Forest Generator prefabs by example:
#   FG_Forest_Spruce1.et   (ID 1 in the SCR_SHPPrefabDataList)
#   FG_Forest_Pine1.et     (ID 2)
# Note the trailing `1` with no underscore separator. The doc doesn't pin
# the directory path; search the Resource Browser for "FG_Forest" to find
# the actual location in your Reforger install. The catalogue below ships
# empty so we never fabricate a path that fails to resolve in Workbench.
#
# Example (confirm against your install before committing):
#   "coniferous": "<dir>/FG_Forest_Pine1.et",
#   "deciduous":  "<dir>/FG_Forest_Deciduous1.et",
#   "mixed":      "<dir>/FG_Forest_Spruce1.et",
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
