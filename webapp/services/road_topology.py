"""
Road topology — turn OSM way fragments into roads with a start and an end.

Issue #161: OSM splits a way whenever any tag changes (a speed limit, a bridge,
a name), so one real road arrives as many fragments. Emitting one spline per way
gave splines that stop in the middle of a road with a visible seam where the
next one starts, and no notion of a main road that side roads join.

Four passes, in order:

1. :func:`stitch_ways` — merge fragments through shared endpoints into
   junction-to-junction roads. A node where three or more way-ends meet is a
   real junction and is never merged through, so merged roads end where the road
   network actually branches.
2. :func:`rank_roads` — score each road by OSM classification (``huvudled``
   first). Only used to decide who snaps to whom.
3. :func:`snap_junctions` — pull a road's loose end onto the exact vertex of a
   more important road within ``ROAD_JUNCTION_SNAP_M``. Sharing the coordinate
   is what removes the seam.
4. :func:`densify` — insert intermediate points so elevation sampling has
   something to sample. Enfusion interpolates straight between control points,
   so sparse splines cut into rising ground and fly over falling ground.

Everything here works in WGS84 lon/lat, because it has to run *before*
elevation is sampled in the projection step. Distances use an equirectangular
approximation, which is accurate well past the 32 km map limit.

All four passes are non-fatal: on any unexpected input they return the data
unchanged rather than losing roads.
"""

from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

from config.roads import (
    ROAD_CLASS_RANK,
    ROAD_JUNCTION_SNAP_M,
    ROAD_MAX_DENSIFIED_POINTS,
    ROAD_MERGE_KEY_PROPS,
    ROAD_POINT_SPACING_M,
    ROAD_RANK_DEFAULT,
)

logger = logging.getLogger(__name__)

# Endpoint coordinates are hashed to this many decimals when matching (≈1 cm).
_NODE_PRECISION = 7

EARTH_RADIUS_M = 6_371_000.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def metres_between(a: Iterable[float], b: Iterable[float]) -> float:
    """Equirectangular distance in metres between two [lon, lat] points."""
    lon1, lat1 = float(a[0]), float(a[1])
    lon2, lat2 = float(b[0]), float(b[1])
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx = math.radians(lon2 - lon1) * math.cos(mean_lat)
    dy = math.radians(lat2 - lat1)
    return math.hypot(dx, dy) * EARTH_RADIUS_M


def _node_key(point: Iterable[float]) -> tuple[float, float]:
    return (round(float(point[0]), _NODE_PRECISION),
            round(float(point[1]), _NODE_PRECISION))


def road_rank(highway_type: Optional[str]) -> int:
    """Importance of a road class — lower is more important."""
    return ROAD_CLASS_RANK.get(highway_type or "", ROAD_RANK_DEFAULT)


# ---------------------------------------------------------------------------
# 1. Stitching
# ---------------------------------------------------------------------------

def _merge_key(props: dict) -> tuple:
    return tuple(str(props.get(k, "") or "") for k in ROAD_MERGE_KEY_PROPS)


def stitch_ways(features: list[dict]) -> list[dict]:
    """
    Merge LineString features that continue each other into single features.

    Two fragments merge when they share an endpoint, agree on every property in
    ``ROAD_MERGE_KEY_PROPS``, and that shared node carries **exactly two**
    way-ends in the whole dataset. The last condition is what keeps junctions
    intact: at a T-junction the node has three way-ends, so the through-road is
    not silently welded to the side road.

    Properties are taken from the first fragment in the chain; ``osm_id``
    becomes a list under ``osm_ids`` so the merged road can still be traced back.
    Non-LineString members pass through untouched.
    """
    lines: list[dict] = []
    passthrough: list[dict] = []
    for f in features or []:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString" and len(coords) >= 2:
            lines.append(f)
        else:
            passthrough.append(f)

    if not lines:
        return list(features or [])

    # Count way-ends per node across the whole dataset (junction detection).
    end_degree: dict[tuple[float, float], int] = {}
    for f in lines:
        coords = f["geometry"]["coordinates"]
        for node in (_node_key(coords[0]), _node_key(coords[-1])):
            end_degree[node] = end_degree.get(node, 0) + 1

    # Index way-ends so we can find the neighbour of a given end quickly.
    ends: dict[tuple[float, float], list[int]] = {}
    for i, f in enumerate(lines):
        coords = f["geometry"]["coordinates"]
        ends.setdefault(_node_key(coords[0]), []).append(i)
        ends.setdefault(_node_key(coords[-1]), []).append(i)

    consumed: set[int] = set()

    def _neighbour(idx: int, node: tuple[float, float]) -> Optional[int]:
        """The single unconsumed, compatible way continuing `idx` at `node`."""
        if end_degree.get(node, 0) != 2:
            return None  # a real junction (or a dead end) — stop here
        for other in ends.get(node, ()):
            if other == idx or other in consumed:
                continue
            if _merge_key(lines[other].get("properties") or {}) != \
                    _merge_key(lines[idx].get("properties") or {}):
                return None
            return other
        return None

    merged: list[dict] = []
    for i, f in enumerate(lines):
        if i in consumed:
            continue
        consumed.add(i)
        coords = [list(p) for p in f["geometry"]["coordinates"]]
        member_ids = [(f.get("properties") or {}).get("osm_id")]

        # Walk forward from the tail, then backward from the head.
        for forward in (True, False):
            while True:
                node = _node_key(coords[-1] if forward else coords[0])
                nxt = _neighbour(i, node)
                if nxt is None:
                    break
                consumed.add(nxt)
                other = [list(p) for p in lines[nxt]["geometry"]["coordinates"]]
                if _node_key(other[0]) != node:
                    other.reverse()
                member_ids.append((lines[nxt].get("properties") or {}).get("osm_id"))
                if forward:
                    coords = coords + other[1:]      # drop the shared node
                else:
                    coords = list(reversed(other[1:])) + coords
                if len(coords) > ROAD_MAX_DENSIFIED_POINTS:
                    break  # pathological ring — stop growing

        props = dict(f.get("properties") or {})
        props["osm_ids"] = [m for m in member_ids if m is not None]
        props["merged_way_count"] = len(member_ids)
        merged.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        })

    junction_nodes = sum(1 for deg in end_degree.values() if deg >= 3)
    logger.info(
        f"Road stitching: {len(lines)} OSM way(s) -> {len(merged)} continuous "
        f"road(s); {junction_nodes} junction node(s) left intact"
    )
    return merged + passthrough


# ---------------------------------------------------------------------------
# 2 + 3. Ranking and junction snapping
# ---------------------------------------------------------------------------

def rank_roads(features: list[dict]) -> list[int]:
    """Rank per feature, lower = more important. Non-roads get the default."""
    return [
        road_rank((f.get("properties") or {}).get("highway"))
        for f in features or []
    ]


def snap_junctions(
    features: list[dict],
    tolerance_m: float = ROAD_JUNCTION_SNAP_M,
) -> int:
    """
    Move each road's loose ends onto the nearest vertex of a *more important*
    road, when one is within ``tolerance_m``. Mutates ``features`` in place and
    returns the number of ends snapped.

    OSM geometry frequently leaves a metre or two of slack where a side road
    meets a main road — which the World Editor renders as a floating stub. After
    snapping, the two splines share a coordinate exactly.

    A main road is never moved to fit a side road, and an end is not snapped if
    the road is too short to survive it.
    """
    lines = [
        f for f in (features or [])
        if (f.get("geometry") or {}).get("type") == "LineString"
        and len(((f.get("geometry") or {}).get("coordinates") or [])) >= 2
    ]
    if len(lines) < 2:
        logger.info(
            "Road junctions: %d road(s) — nothing to snap against", len(lines)
        )
        return 0

    ranks = rank_roads(lines)

    # Spatial hash of every vertex, bucketed at roughly the snap tolerance.
    # Degrees-per-metre varies with latitude; use the dataset's mean latitude.
    all_lats = [c[1] for f in lines for c in f["geometry"]["coordinates"][:1]]
    mean_lat = sum(all_lats) / len(all_lats) if all_lats else 0.0
    deg_per_m_lat = 1.0 / 110_540.0
    deg_per_m_lon = 1.0 / max(1.0, 111_320.0 * math.cos(math.radians(mean_lat)))
    cell_lat = tolerance_m * deg_per_m_lat
    cell_lon = tolerance_m * deg_per_m_lon

    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for li, f in enumerate(lines):
        for ci, c in enumerate(f["geometry"]["coordinates"]):
            key = (int(c[0] / cell_lon), int(c[1] / cell_lat))
            grid.setdefault(key, []).append((li, ci))

    snapped = 0
    for li, f in enumerate(lines):
        coords = f["geometry"]["coordinates"]
        for end_idx in (0, -1):
            # The vertex the moving end is anchored to. A two-point side road
            # (very common in OSM — a straight stub off a main road) has to be
            # snappable too, so the guard is "don't collapse the segment",
            # not "must have three points".
            anchor = coords[1] if end_idx == 0 else coords[-2]
            point = coords[end_idx]
            gx, gy = int(point[0] / cell_lon), int(point[1] / cell_lat)
            best: Optional[tuple[float, list[float]]] = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for (oli, oci) in grid.get((gx + dx, gy + dy), ()):
                        if oli == li or ranks[oli] >= ranks[li]:
                            continue  # only snap onto a more important road
                        cand = lines[oli]["geometry"]["coordinates"][oci]
                        d = metres_between(point, cand)
                        if d <= tolerance_m and (best is None or d < best[0]):
                            best = (d, cand)
            if best is None or best[0] == 0.0:
                continue
            if metres_between(anchor, best[1]) < 1.0:
                continue  # snapping would collapse the segment
            coords[end_idx] = [float(best[1][0]), float(best[1][1])]
            snapped += 1

    logger.info(
        f"Road junctions: checked {len(lines) * 2} road end(s) against a "
        f"{tolerance_m:.0f} m tolerance — snapped {snapped} onto a more "
        f"important road"
    )
    return snapped


# ---------------------------------------------------------------------------
# 4. Densification
# ---------------------------------------------------------------------------

def densify(
    coords: list[list[float]],
    max_spacing_m: float = ROAD_POINT_SPACING_M,
    max_points: int = ROAD_MAX_DENSIFIED_POINTS,
) -> list[list[float]]:
    """
    Insert intermediate points so no segment is longer than *max_spacing_m*.

    Runs before projection so each inserted point gets a real DEM elevation.
    Straight-line interpolation in lon/lat is exact enough: at these spacings
    the great-circle deviation is sub-millimetre.
    """
    if len(coords) < 2 or max_spacing_m <= 0:
        return coords

    total = sum(
        metres_between(coords[i], coords[i + 1]) for i in range(len(coords) - 1)
    )
    # Densifying beyond the budget would only be thrown away by the simplifier,
    # so widen the spacing instead of truncating the road.
    spacing = max(max_spacing_m, total / max(1, max_points - len(coords)))

    out: list[list[float]] = [list(coords[0])]
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        seg = metres_between(a, b)
        if seg > spacing:
            steps = int(seg // spacing)
            for s in range(1, steps + 1):
                t = s / (steps + 1)
                out.append([
                    a[0] + (b[0] - a[0]) * t,
                    a[1] + (b[1] - a[1]) * t,
                ])
        out.append(list(b))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_road_network(
    features: list[dict],
    snap_tolerance_m: float = ROAD_JUNCTION_SNAP_M,
    spacing_m: float = ROAD_POINT_SPACING_M,
) -> tuple[list[dict], dict]:
    """
    Run the full topology pipeline and return ``(features, stats)``.

    Any failure returns the input unchanged with ``stats["error"]`` set —
    a road network that is merely fragmented still beats no roads at all.
    """
    stats = {
        "input_ways": len(features or []),
        "merged_roads": 0,
        "snapped_ends": 0,
        "points_before": 0,
        "points_after": 0,
    }
    try:
        stats["points_before"] = sum(
            len((f.get("geometry") or {}).get("coordinates") or [])
            for f in features or []
        )
        merged = stitch_ways(features)
        stats["merged_roads"] = len(merged)
        stats["snapped_ends"] = snap_junctions(merged, snap_tolerance_m)
        for f in merged:
            geom = f.get("geometry") or {}
            if geom.get("type") == "LineString":
                geom["coordinates"] = densify(geom["coordinates"], spacing_m)
        stats["points_after"] = sum(
            len((f.get("geometry") or {}).get("coordinates") or [])
            for f in merged
        )
        logger.info(
            f"Road densification: {stats['points_before']} -> "
            f"{stats['points_after']} control point(s) at {spacing_m:.0f} m "
            f"spacing, so each one samples a real terrain height"
        )
        return merged, stats
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Road topology pass failed ({exc}); using raw OSM ways")
        stats["error"] = str(exc)
        return list(features or []), stats
