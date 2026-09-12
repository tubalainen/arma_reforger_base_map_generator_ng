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

# ---------------------------------------------------------------------------
# Sea / ocean bathymetry (issue #193)
# ---------------------------------------------------------------------------
# A sea is not a big lake. Lakes get one linear ramp from the shore; a coast
# has a shallow shelf you can wade and swim off, then a drop to real depth.
# The reporter asked for exactly that: "a shoreline for a realistic number of
# metres where the depth gradually increases up to max 10 horizontal metres
# and 2 vertical metres off shore, where the depth drops off dynamically to
# 30-100 metres".
#
#   depth(d) =  SEA_SHELF_DEPTH_M * d / SEA_SHELF_WIDTH_M          d <= shelf
#               SEA_SHELF_DEPTH_M + (d - shelf) * dropoff_slope     d >  shelf
#   capped at the region's own max depth.

# The wadeable shelf: 2 m down over the first 10 m out from the shore.
SEA_SHELF_WIDTH_M = 10.0
SEA_SHELF_DEPTH_M = 2.0

# Past the shelf the floor falls away. 0.5 m/m puts a region 100 m from shore
# at ~47 m, and one 200 m out at the 100 m ceiling.
SEA_DROPOFF_SLOPE_M_PER_M = 0.5

# "Dynamically to 30-100 m": each sea region gets its own ceiling, scaled by
# how far offshore it actually reaches, so a narrow strait does not get an
# abyss and open ocean is not capped at wading depth.
SEA_MIN_DEPTH_M = 30.0
SEA_MAX_DEPTH_M = 100.0

# Offshore distance at which a region earns the full SEA_MAX_DEPTH_M ceiling.
# Below it the ceiling is interpolated from SEA_MIN_DEPTH_M.
SEA_FULL_DEPTH_DISTANCE_M = 1500.0

# "Only where there clearly is a larger body of water." Two independent
# guards, both of which must hold, so an inland map never gets an ocean floor
# carved into a big lake:
#   * the region must cover at least this fraction of the map, and
#   * it must touch the map edge (a sea continues past the selection; a lake
#     that fits entirely inside the map is a lake, however large).
SEA_MIN_AREA_FRACTION = 0.02
SEA_MUST_TOUCH_MAP_EDGE = True

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
