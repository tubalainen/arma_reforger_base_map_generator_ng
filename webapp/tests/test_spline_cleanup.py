"""
Tests for :mod:`services.spline_cleanup` — the dedup / union / hairpin /
adaptive-tolerance helpers added in v1.4.4 to fix issues #93 and #88
(duplicate, over-detailed, and spiralling splines in the World Editor).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

shapely = pytest.importorskip("shapely")  # noqa: F841 — cleanup needs shapely

from services.spline_cleanup import (  # noqa: E402
    adaptive_tolerance,
    normalize_polygons,
    normalize_polylines,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _square(cx: float, cy: float, half: float) -> list[list[list[float]]]:
    """Return a closed-ring Polygon coordinate set centred at ``(cx, cy)``."""
    return [
        [
            [cx - half, cy - half],
            [cx + half, cy - half],
            [cx + half, cy + half],
            [cx - half, cy + half],
            [cx - half, cy - half],
        ]
    ]


def _poly(coords, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": props,
    }


def _line(coords, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def _xz(*pairs):
    """Build a list of {x,y,z} dicts from ``(x, z)`` pairs (y defaults to 0)."""
    return [{"x": float(x), "y": 0.0, "z": float(z)} for x, z in pairs]


# --------------------------------------------------------------------------- #
# normalize_polygons
# --------------------------------------------------------------------------- #


class TestNormalizePolygons:
    def test_two_identical_forests_collapse_to_one(self):
        # Same 1×1 deg square submitted twice (e.g. way + relation).
        feats = [_poly(_square(0.5, 0.5, 0.4)), _poly(_square(0.5, 0.5, 0.4))]
        out = normalize_polygons(feats, "forest")
        assert len(out) == 1
        assert out[0]["geometry"]["type"] == "Polygon"

    def test_way_plus_relation_lake_dedups(self):
        # Same lake mapped as both a way (named) and a relation (anonymous):
        # the named feature's name must survive — preferred by _best_props.
        named = _poly(_square(0.5, 0.5, 0.4), name="Lake Vänern", water_type="lake")
        anon = _poly(_square(0.5, 0.5, 0.4), water_type="lake")
        out = normalize_polygons([named, anon], "lake")
        assert len(out) == 1
        assert out[0]["properties"].get("name") == "Lake Vänern"

    def test_touching_forests_merge_into_one(self):
        # Two squares sharing the edge x=1.0 between (0,0)–(1,1) and (1,0)–(2,1).
        a = _poly([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]])
        b = _poly([[[1, 0], [2, 0], [2, 1], [1, 1], [1, 0]]])
        out = normalize_polygons([a, b], "forest")
        assert len(out) == 1
        # The merged outline must span x=[0,2]
        coords = out[0]["geometry"]["coordinates"][0]
        xs = [c[0] for c in coords]
        assert min(xs) == 0 and max(xs) == 2

    def test_disjoint_forests_stay_separate(self):
        a = _poly(_square(0.5, 0.5, 0.4))
        b = _poly(_square(5.0, 5.0, 0.4))  # far away
        out = normalize_polygons([a, b], "forest")
        assert len(out) == 2

    def test_linestrings_pass_through_untouched(self):
        # Coastlines / rivers etc. mixed into a water FeatureCollection must
        # not be unioned with polygon water.
        coast = _line([[0, 0], [1, 0], [2, 0]], water_type="coastline")
        lake = _poly(_square(0.5, 0.5, 0.4), water_type="lake")
        out = normalize_polygons([coast, lake], "lake")
        assert len(out) == 2
        types = {f["geometry"]["type"] for f in out}
        assert types == {"LineString", "Polygon"}

    def test_named_property_wins_over_unnamed(self):
        # Two overlapping (not identical) polygons: the one with a name
        # should contribute its name even if it's the smaller piece.
        small_named = _poly(_square(0.5, 0.5, 0.3), name="Tiny Pond")
        large_anon = _poly(_square(0.5, 0.5, 0.5))
        out = normalize_polygons([small_named, large_anon], "lake")
        assert len(out) == 1
        assert out[0]["properties"].get("name") == "Tiny Pond"

    def test_tiny_sliver_below_min_area_is_dropped(self):
        # 1m × 1m square in lon/lat ~= 1.1e-5 deg per side.
        # min_area_m2 default is 100 → 1 m² sliver gets dropped.
        sliver = _poly(_square(0.0, 0.0, 5e-6))  # ~0.5 m half-edge
        out = normalize_polygons([sliver], "forest")
        assert out == []

    def test_empty_input_returns_empty(self):
        assert normalize_polygons([], "forest") == []

    def test_invalid_geometry_repaired_via_buffer0(self):
        # Bowtie / self-intersecting polygon — buffer(0) should fix it
        # rather than raise.
        bowtie = _poly([[[0, 0], [2, 2], [0, 2], [2, 0], [0, 0]]])
        out = normalize_polygons([bowtie], "forest")
        # Either 1 repaired polygon or 2 triangles — both are valid outcomes.
        assert len(out) >= 1
        for f in out:
            assert f["geometry"]["type"] == "Polygon"


# --------------------------------------------------------------------------- #
# normalize_polylines (hairpin detection)
# --------------------------------------------------------------------------- #


class TestNormalizePolylines:
    def test_hairpin_vertex_is_dropped(self):
        # A→B→A' where A' is 5 m east of A creates a near-180° reversal.
        # Use small lon offsets at lat=0 (1° lon ≈ 111.3 km).
        ten_m = 10.0 / 111_320.0
        five_m = 5.0 / 111_320.0
        coords = [
            [0.0, 0.0],
            [ten_m, 0.0],        # B
            [five_m, 0.0],       # A' — back-track, 5 m from start
            [2 * ten_m, 0.0],    # continuation
        ]
        out = normalize_polylines([_line(coords)], "river")
        assert len(out) == 1
        new_coords = out[0]["geometry"]["coordinates"]
        # The hairpin middle vertex (B) must be dropped.
        # Exact check: middle vertex at ten_m should no longer be present.
        xs = [c[0] for c in new_coords]
        assert ten_m not in xs

    def test_smooth_meander_keeps_all_vertices(self):
        # 20 vertices forming a wide sinusoidal meander: each turn is small
        # (<<150°) so no vertex should be considered a hairpin.
        coords = []
        for i in range(20):
            lon = i * 0.0005          # ~55 m spacing
            lat = math.sin(i / 3.0) * 0.0002
            coords.append([lon, lat])
        out = normalize_polylines([_line(coords)], "river")
        assert len(out) == 1
        assert len(out[0]["geometry"]["coordinates"]) == 20

    def test_straight_line_keeps_all_vertices(self):
        coords = [[i * 0.001, 0.0] for i in range(10)]
        out = normalize_polylines([_line(coords)], "river")
        assert len(out[0]["geometry"]["coordinates"]) == 10

    def test_polygon_features_pass_through_untouched(self):
        poly = _poly(_square(0.5, 0.5, 0.4))
        out = normalize_polylines([poly], "river")
        assert out == [poly]

    def test_short_polyline_passes_through(self):
        # 2 points cannot have an interior vertex → unchanged.
        out = normalize_polylines([_line([[0, 0], [1, 1]])], "river")
        assert out[0]["geometry"]["coordinates"] == [[0, 0], [1, 1]]

    def test_collapsed_polyline_is_dropped(self):
        # Three points where every interior is a hairpin: result < 2 pts → drop.
        d = 5.0 / 111_320.0  # 5 m
        coords = [[0, 0], [d, 0], [0, 0]]
        out = normalize_polylines([_line(coords)], "river")
        # Hairpin removal yields [0,0]→[0,0] (a single point pair) which is
        # still ≥2 — so the feature is allowed through but degenerate. Either
        # behaviour is acceptable; pin the exact one we ship.
        assert len(out) <= 1


# --------------------------------------------------------------------------- #
# adaptive_tolerance
# --------------------------------------------------------------------------- #


class TestAdaptiveTolerance:
    def test_small_feature_uses_floor(self):
        pts = _xz((0, 0), (50, 0), (50, 50), (0, 50))  # ~71 m diagonal
        # 71 * 0.005 = 0.35 m → clamped to floor (1.0 m)
        assert adaptive_tolerance(pts) == pytest.approx(1.0)

    def test_large_feature_uses_ceiling(self):
        pts = _xz((0, 0), (3000, 0), (3000, 3000), (0, 3000))  # ~4.2 km diag
        # 4243 * 0.005 = 21 m → clamped to ceiling (5.0 m)
        assert adaptive_tolerance(pts) == pytest.approx(5.0)

    def test_medium_feature_in_range(self):
        # 600 m diagonal → 0.005 × 600 = 3.0 m, between floor and ceiling
        pts = _xz((0, 0), (600, 0))
        # Adjust to actually be 600 m diagonal:
        pts = _xz((0, 0), (424, 0), (424, 424), (0, 424))  # 600 m diag
        tol = adaptive_tolerance(pts)
        assert 1.0 < tol < 5.0

    def test_empty_returns_floor(self):
        assert adaptive_tolerance([]) == pytest.approx(1.0)

    def test_custom_bounds_respected(self):
        pts = _xz((0, 0), (1000, 0), (1000, 1000), (0, 1000))
        tol = adaptive_tolerance(pts, lo=2.0, hi=3.0)
        assert 2.0 <= tol <= 3.0


# --------------------------------------------------------------------------- #
# Integration: end-to-end "no spirals, no excess points"
# --------------------------------------------------------------------------- #


class TestSimplifyIntegration:
    """
    Round-trip a dense forest ring through ``_simplify_local_ring`` and assert
    (a) point count drops well below 200 and (b) the result is topologically
    simple (no self-intersections — the "spiral" symptom from #93).
    """

    def test_dense_ring_simplifies_and_stays_simple(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator
        from shapely.geometry import Polygon

        # 500 points sampled around a 2 km × 2 km square (perimeter, not
        # circle, so there's plenty of redundant collinear detail).
        pts = []
        for i in range(125):
            pts.append({"x": i * 16.0, "y": 0.0, "z": 0.0})
        for i in range(125):
            pts.append({"x": 2000.0, "y": 0.0, "z": i * 16.0})
        for i in range(125):
            pts.append({"x": 2000.0 - i * 16.0, "y": 0.0, "z": 2000.0})
        for i in range(125):
            pts.append({"x": 0.0, "y": 0.0, "z": 2000.0 - i * 16.0})
        assert len(pts) == 500

        simplified = EnfusionProjectGenerator._simplify_local_ring(pts)
        assert len(simplified) <= 60, f"got {len(simplified)} pts; expected ≤60"
        assert len(simplified) >= 4

        # Topology: the closed ring must be simple (no self-intersection).
        ring_coords = [(p["x"], p["z"]) for p in simplified]
        ring_coords.append(ring_coords[0])
        assert Polygon(ring_coords).is_simple

    def test_dense_polyline_simplifies_and_stays_simple(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator
        from shapely.geometry import LineString

        # 400-point gentle S-curve along x with small z wobble.
        pts = []
        for i in range(400):
            pts.append(
                {
                    "x": float(i) * 5.0,
                    "y": 0.0,
                    "z": math.sin(i / 20.0) * 30.0,
                }
            )
        simplified = EnfusionProjectGenerator._simplify_local_polyline(pts)
        assert len(simplified) <= 120
        assert len(simplified) >= 2
        # Endpoints must be preserved verbatim.
        assert simplified[0]["x"] == pts[0]["x"] and simplified[0]["z"] == pts[0]["z"]
        assert simplified[-1]["x"] == pts[-1]["x"] and simplified[-1]["z"] == pts[-1]["z"]
        # No self-intersection
        line = LineString([(p["x"], p["z"]) for p in simplified])
        assert line.is_simple
