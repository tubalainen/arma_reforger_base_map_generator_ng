"""
Lake / water-body classification + Enfusion Lake Generator prefab catalog.

Same architecture as config.buildings and config.forests: catalog ships empty.
Populate KNOWN_LAKE_PREFABS with confirmed LG_*.et paths from a stock
Reforger install and the water layer will auto-attach the generator child to
every matching lake/pond/reservoir spline on the next generation.
"""

from __future__ import annotations

# water_type → Enfusion LG_*.et path. Empty by default.
# Keys match the water_type OSM property values used in the water layer.
# Valid keys: "lake", "pond", "reservoir", "water"
#
# Example (confirm paths against your Reforger install before committing):
#   "lake":      "Prefabs/WEGenerators/Water/Lake/LG_Lake_01.et",
#   "pond":      "Prefabs/WEGenerators/Water/Lake/LG_Lake_Small_01.et",
#   "reservoir": "Prefabs/WEGenerators/Water/Lake/LG_Lake_01.et",
#   "water":     "Prefabs/WEGenerators/Water/Lake/LG_Lake_01.et",
KNOWN_LAKE_PREFABS: dict[str, str] = {}


def validate_lake_prefab(water_type: str | None) -> str | None:
    """
    Return the verified LG_*.et path for a water body type, or None if not cataloged.
    None signals the water-layer emitter to fall back to spline-only mode.
    """
    if not water_type:
        return None
    return KNOWN_LAKE_PREFABS.get(water_type)
