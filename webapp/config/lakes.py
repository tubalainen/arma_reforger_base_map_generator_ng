"""
Lake / water-body classification + Enfusion Lake Generator prefab catalog.

Same architecture as config.buildings and config.forests: catalog ships empty.
Populate KNOWN_LAKE_PREFABS with confirmed LG_*.et paths from a stock
Reforger install and the water layer will auto-attach the generator child to
every matching lake/pond/reservoir spline on the next generation.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Lake geometry / bathymetry tuning (issue #160, closes #106)
# ---------------------------------------------------------------------------
# Reporter feedback: generated lakes are "för grunt" (too shallow) and the
# splines "för små" (too small) — there is no margin to work with when placing
# a Lake Generator, and small inland lakes barely dip below the shoreline.
#
# LAKE_RING_BUFFER_M pushes every standing-water ring outward before it is
# emitted as a spline, so the generator has room and the user isn't hand-editing
# each water spline. Applied in projected local metres, after elevation
# sampling and before terrain clipping, so the buffer is exact and can't push
# a ring outside the map.
LAKE_RING_BUFFER_M = 5.0

# Deepest point of a lake, in metres below its water surface. The old value of
# 8 m was rarely reached (see LAKE_SHORE_SLOPE_M_PER_M).
LAKE_MAX_DEPTH_M = 15.0

# Depth gained per metre of distance from shore. This — not LAKE_MAX_DEPTH_M —
# is what made small lakes shallow: depth ramps linearly from the shore, so at
# the old 0.3 m/m a lake had to be ~27 m from shore to centre before it reached
# 8 m. At 0.5 m/m a 30 m-wide pond reaches 7.5 m at its centre.
LAKE_SHORE_SLOPE_M_PER_M = 0.5

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
