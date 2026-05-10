"""
Building classification + Enfusion prefab catalog.

Same architecture as ``config.roads``: a known-good prefab catalog plus a
validator. The audit (Phase 2 / task A2) explicitly flagged that we do not
ship a verified list of stock-Reforger ``Building_*.et`` prefab paths, so
this catalog starts EMPTY.

When the catalog is empty the buildings layer falls back to emitting a
visible footprint outline per real-world building (the same closed-spline
pattern used for forests/lakes), giving the user pre-positioned markers to
drag prefabs onto. Once the user (or a future contributor with Workbench
access) confirms prefab paths, populate ``KNOWN_BUILDING_PREFABS`` and the
generator automatically upgrades to fully auto-placed entities — no code
change required.

Building category labels come from ``feature_extractor.extract_building_features``
which maps OSM ``building`` tags to the categories below.
"""

from __future__ import annotations

# Path under the ArmaReforger data root where building prefabs live.
# Kept generic on purpose — confirm against your install before committing
# specific subdirectories to ``KNOWN_BUILDING_PREFABS``.
BUILDING_PREFAB_BASE = "Prefabs/Structures"

# Known-good (category -> .et path) mappings. Empty by default so we never
# fabricate paths that fail to resolve in Workbench. Add an entry only after
# you have confirmed the path exists in a stock Reforger install.
#
# Example future entry:
#     "Building_House": "Prefabs/Structures/Civilian/SmallHouse_01.et",
#
# The category strings are produced by feature_extractor.extract_building_features
# and currently include:
#     Building_Apartments, Building_Barn, Building_Church, Building_Commercial,
#     Building_Garage, Building_Generic, Building_House, Building_Industrial,
#     Building_Residential, Building_Shed
KNOWN_BUILDING_PREFABS: dict[str, str] = {}


def validate_building_prefab(category: str | None) -> str | None:
    """
    Look up the verified Enfusion prefab path for a building category.

    Returns the full ``Prefabs/.../Whatever.et`` string if the category has a
    confirmed mapping in ``KNOWN_BUILDING_PREFABS``, otherwise returns ``None``.

    The buildings-layer emitter uses ``None`` as a signal to fall back to
    footprint-outline mode (a visible spline marker the user can wire
    manually) rather than fabricating a prefab path.
    """
    if not category:
        return None
    return KNOWN_BUILDING_PREFABS.get(category)
