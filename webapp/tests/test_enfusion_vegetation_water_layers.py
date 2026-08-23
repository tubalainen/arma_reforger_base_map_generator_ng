"""
Tests for the auto-emitted vegetation.layer and water.layer in
EnfusionProjectGenerator (added in v1.0.6).

Pre-v1.0.6 these layers were single-comment placeholders. Now they emit one
closed `SplineShapeEntity` per forest / lake polygon.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


# ---------------------------------------------------------------------------
# Fixtures: a minimal stand-in for CoordinateTransformer so we don't need
# pyproj installed locally to run these tests.
# ---------------------------------------------------------------------------

class _IdentityTransformer:
    """
    Minimal transformer that maps WGS84 lon/lat directly to local x/z by
    multiplying by 1000 (so distances in the tests remain visually trackable).
    Y comes back as 0 (no elevation sampling). Matches the public surface
    of services.coordinate_transformer.CoordinateTransformer that
    EnfusionProjectGenerator uses (only `transform_points` is called).
    """

    def transform_points(self, points, elevation_array=None):
        return [
            {"x": round(p["x"] * 1000, 3), "y": 0.0, "z": round(p["y"] * 1000, 3)}
            for p in points
        ]


def _metadata_for_4km_terrain() -> dict:
    """Standard 2049x2049 vertex / 4096m terrain metadata."""
    return {
        "heightmap": {"dimensions": "2049x2049", "grid_cell_size_m": 2.0},
        "elevation": {
            "min_elevation_m": 0,
            "max_elevation_m": 100,
            "height_scale": 0.03125,
            "height_offset": 0,
        },
        "input": {"bbox": {"south": 0.0, "north": 4.0, "west": 0.0, "east": 4.0}},
    }


def _polygon_feature(coords, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": props,
    }


def _multipolygon_feature(coords, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "MultiPolygon", "coordinates": coords},
        "properties": props,
    }


@pytest.fixture
def generator_factory():
    """Construct an EnfusionProjectGenerator with our identity transformer."""
    from services.enfusion_project_generator import EnfusionProjectGenerator

    def _make(forest_features=None, water_features=None):
        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            transformer=_IdentityTransformer(),
            elevation_array=None,
            forest_features=forest_features,
            water_features=water_features,
        )
        gen._reset_naming_state()
        return gen
    return _make


# ---------------------------------------------------------------------------
# Vegetation layer tests
# ---------------------------------------------------------------------------

class TestVegetationLayer:
    def test_no_forest_features_emits_empty_message(self, generator_factory):
        gen = generator_factory(forest_features=None)
        out = gen._generate_vegetation_layer()
        assert "// Vegetation layer" in out  # header still present
        assert "// No forest polygons" in out
        assert "SplineShapeEntity" not in out

    def test_single_polygon_emits_one_closed_spline(self, generator_factory):
        # Square 1×1 polygon centered around (1, 1) WGS84 → (1000, 1000) local
        ring = [
            [0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]
        ]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [_polygon_feature([ring], type="forest")],
        })
        out = gen._generate_vegetation_layer()

        # v1.4.0 — descriptive names (Forest_<type>_<quadrant>_NNN).
        # Fixture has no leaf_type → forest_type defaults to "mixed" → token "Mixed".
        assert out.count("SplineShapeEntity Forest_") == 1
        assert "SplineShapeEntity Forest_Mixed_" in out
        # 4 unique points + 1 repeat for the close = 5 ShapePoint blocks
        assert out.count("ShapePoint sp_") == 5
        assert "ShapePoint sp_0" in out and "ShapePoint sp_4" in out
        # The first and last shape points must be at the same relative position (closed)
        first_pos = "Position 0.000 0.000 0.000"
        assert out.count(first_pos) >= 2

    def test_multipolygon_emits_one_spline_per_subpolygon(self, generator_factory):
        ring_a = [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0], [0.5, 0.5]]
        ring_b = [[2.0, 2.0], [2.5, 2.0], [2.5, 2.5], [2.0, 2.5], [2.0, 2.0]]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [_multipolygon_feature([[ring_a], [ring_b]], type="forest")],
        })
        out = gen._generate_vegetation_layer()
        # v1.4.0 — two anonymous mixed forest polygons get two
        # Forest_Mixed_<quadrant>_NNN entries.
        assert out.count("SplineShapeEntity Forest_") == 2

    def test_polygon_with_holes_uses_only_exterior_ring(self, generator_factory):
        # Outer square + inner hole — hole must be ignored (splines can't have holes)
        outer = [[0.5, 0.5], [3.5, 0.5], [3.5, 3.5], [0.5, 3.5], [0.5, 0.5]]
        hole = [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [_polygon_feature([outer, hole], type="forest")],
        })
        out = gen._generate_vegetation_layer()
        # One spline entity (not two — the hole is dropped)
        assert out.count("SplineShapeEntity Forest_") == 1
        # And it has 5 ShapePoints (4 outer + 1 close), not 9 or more
        assert out.count("ShapePoint sp_") == 5

    def test_polygon_outside_terrain_is_skipped(self, generator_factory):
        # Identity transformer × 1000: this polygon ends up at (10000, 10000) etc.
        # Terrain is 4096m wide → fully out of bounds.
        far_ring = [[10.0, 10.0], [11.0, 10.0], [11.0, 11.0], [10.0, 11.0], [10.0, 10.0]]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [_polygon_feature([far_ring], type="forest")],
        })
        out = gen._generate_vegetation_layer()
        assert "SplineShapeEntity" not in out
        assert "// No forest polygons" in out

    def test_no_transformer_emits_friendly_comment(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator
        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            transformer=None,
            forest_features={
                "type": "FeatureCollection",
                "features": [_polygon_feature(
                    [[[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0], [0.5, 0.5]]],
                    type="forest",
                )],
            },
        )
        out = gen._generate_vegetation_layer()
        assert "Coordinate transformer unavailable" in out
        assert "SplineShapeEntity" not in out


import re


def _abs_shapepoints(layer_text: str) -> list[tuple[float, float]]:
    """Return absolute (x, z) of every ShapePoint across all entities."""
    pts: list[tuple[float, float]] = []
    for block in re.split(r"SplineShapeEntity ", layer_text)[1:]:
        m = re.search(r"coords\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)", block)
        if not m:
            continue
        ox, oz = float(m.group(1)), float(m.group(3))
        for pm in re.finditer(
            r"Position\s+([\-\d.]+)\s+([\-\d.]+)\s+([\-\d.]+)", block
        ):
            pts.append((ox + float(pm.group(1)), oz + float(pm.group(3))))
    return pts


class TestForestClipping:
    """Issue #139 — forests crossing the terrain edge must be clipped to the
    terrain rectangle (follow the edge), NOT point-filtered and re-closed with a
    chord across the map."""

    # Identity transformer ×1000, terrain is 4096 m → bound + 1 m margin.
    BOUND = 4096 + 1.0

    def test_boundary_crossing_forest_is_clipped_in_bounds(self, generator_factory):
        # Spans lon 1..6 → local x 1000..6000; the east edge is at 4096.
        ring = [[1.0, 1.0], [6.0, 1.0], [6.0, 3.0], [1.0, 3.0], [1.0, 1.0]]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [_polygon_feature([ring], type="forest")],
        })
        out = gen._generate_vegetation_layer()
        assert "SplineShapeEntity Forest_" in out  # not skipped
        pts = _abs_shapepoints(out)
        assert pts, "expected at least one ShapePoint"
        # No vertex may sit near the original far-outside x=6000 corner.
        assert max(x for x, _ in pts) <= self.BOUND + 1e-3

    def test_clip_helper_returns_bounded_rings(self, generator_factory):
        gen = generator_factory()
        local = [
            {"x": 1000.0, "y": 0.0, "z": 1000.0},
            {"x": 6000.0, "y": 0.0, "z": 1000.0},
            {"x": 6000.0, "y": 0.0, "z": 3000.0},
            {"x": 1000.0, "y": 0.0, "z": 3000.0},
        ]
        rings = gen._clip_ring_to_terrain(local)
        assert rings
        for ring in rings:
            for p in ring:
                assert -1.001 <= p["x"] <= self.BOUND + 1e-3
                assert -1.001 <= p["z"] <= self.BOUND + 1e-3


class TestSimpleRingGuarantee:
    """Issue #105 — never emit a self-intersecting ring (trees spray outside)."""

    def test_valid_square_passes_through(self, generator_factory):
        gen = generator_factory()
        sq = [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 10.0, "y": 0.0, "z": 0.0},
            {"x": 10.0, "y": 0.0, "z": 10.0},
            {"x": 0.0, "y": 0.0, "z": 10.0},
        ]
        out = gen._ensure_simple_ring(sq)
        assert out is not None and len(out) == 4

    def test_bowtie_is_repaired_or_dropped(self, generator_factory):
        from shapely.geometry import Polygon
        gen = generator_factory()
        # A classic self-intersecting "bowtie".
        bowtie = [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 10.0, "y": 0.0, "z": 10.0},
            {"x": 10.0, "y": 0.0, "z": 0.0},
            {"x": 0.0, "y": 0.0, "z": 10.0},
        ]
        out = gen._ensure_simple_ring(bowtie)
        # Either dropped, or returned as a valid simple polygon — never invalid.
        if out is not None:
            poly = Polygon([(p["x"], p["z"]) for p in out])
            assert poly.is_valid


# ---------------------------------------------------------------------------
# Water layer tests
# ---------------------------------------------------------------------------

class TestWaterLayer:
    def test_lake_polygon_is_emitted(self, generator_factory):
        ring = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        gen = generator_factory(water_features={
            "type": "FeatureCollection",
            "features": [_polygon_feature([ring], water_type="lake")],
        })
        out = gen._generate_water_layer()
        # v1.4.0 — anonymous lake gets Lake_<quadrant>_NNN.
        assert out.count("SplineShapeEntity Lake_") == 1

    def test_river_polygon_is_filtered_out(self, generator_factory):
        # water_type=river is excluded — only standing-water types qualify
        ring = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        gen = generator_factory(water_features={
            "type": "FeatureCollection",
            "features": [_polygon_feature([ring], water_type="river")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity" not in out
        assert "// No standing-water polygons" in out

    def test_mixed_features_only_emits_lake_like_polygons(self, generator_factory):
        lake_ring = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        pond_ring = [[2.0, 2.0], [2.5, 2.0], [2.5, 2.5], [2.0, 2.5], [2.0, 2.0]]
        river_ring = [[3.0, 3.0], [3.5, 3.0], [3.5, 3.5], [3.0, 3.5], [3.0, 3.0]]
        gen = generator_factory(water_features={
            "type": "FeatureCollection",
            "features": [
                _polygon_feature([lake_ring], water_type="lake"),
                _polygon_feature([pond_ring], water_type="pond"),
                _polygon_feature([river_ring], water_type="river"),
            ],
        })
        out = gen._generate_water_layer()
        # v1.4.0 — two anonymous lake/pond polygons → two Lake_* / Pond_*
        # entries; river polygon is still filtered out (Lake naming kind
        # applies because filter_values keeps standing-water only).
        assert out.count("SplineShapeEntity Lake_") + out.count("SplineShapeEntity Pond_") == 2

    def test_no_water_features_emits_empty_message(self, generator_factory):
        gen = generator_factory(water_features=None)
        out = gen._generate_water_layer()
        assert "// Water layer" in out
        assert "// No standing-water polygons" in out
        assert "SplineShapeEntity" not in out


# ---------------------------------------------------------------------------
# Dedup / cleanup integration (issues #93, #88 — v1.4.4)
# ---------------------------------------------------------------------------

class TestSplineCleanupIntegration:
    """
    End-to-end: confirm that duplicate / overlapping inputs collapse to a
    single spline entity once they pass through the new normalize_polygons
    step in the vegetation and water layers.
    """

    pytest.importorskip("shapely")

    def test_duplicate_forests_collapse_to_one_spline(self, generator_factory):
        # Same square submitted twice (e.g. OSM way + relation duplicate).
        ring = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [
                _polygon_feature([ring], type="forest"),
                _polygon_feature([ring], type="forest"),
            ],
        })
        out = gen._generate_vegetation_layer()
        # Despite 2 input features, only 1 spline survives.
        assert out.count("SplineShapeEntity Forest_") == 1

    def test_touching_forests_merge_into_one_spline(self, generator_factory):
        a = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        b = [[1.5, 0.5], [2.5, 0.5], [2.5, 1.5], [1.5, 1.5], [1.5, 0.5]]
        gen = generator_factory(forest_features={
            "type": "FeatureCollection",
            "features": [
                _polygon_feature([a], type="forest"),
                _polygon_feature([b], type="forest"),
            ],
        })
        out = gen._generate_vegetation_layer()
        assert out.count("SplineShapeEntity Forest_") == 1

    def test_duplicate_lakes_collapse_to_one_spline(self, generator_factory):
        ring = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
        gen = generator_factory(water_features={
            "type": "FeatureCollection",
            "features": [
                _polygon_feature([ring], water_type="lake"),
                _polygon_feature([ring], water_type="lake"),
            ],
        })
        out = gen._generate_water_layer()
        assert out.count("SplineShapeEntity Lake_") == 1


# ---------------------------------------------------------------------------
# Lake ring buffer + bathymetry (issue #160, closes #106)
# ---------------------------------------------------------------------------

class TestLakeRingBuffer:
    """Reporter feedback on #160: lake splines hug the OSM shoreline exactly,
    leaving no margin to place a Lake Generator, and the water is too shallow.
    """

    @staticmethod
    def _ring_area(pts):
        """Shoelace area of a projected ring (local metres)."""
        area = 0.0
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            area += a["x"] * b["z"] - b["x"] * a["z"]
        return abs(area) / 2.0

    def test_buffer_grows_the_ring(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator

        square = [
            {"x": 1000.0, "y": 5.0, "z": 1000.0},
            {"x": 1100.0, "y": 5.0, "z": 1000.0},
            {"x": 1100.0, "y": 5.0, "z": 1100.0},
            {"x": 1000.0, "y": 5.0, "z": 1100.0},
        ]
        grown = EnfusionProjectGenerator._buffer_local_ring(square, 5.0)
        assert self._ring_area(grown) > self._ring_area(square)
        # A 100x100 m square buffered by 5 m becomes 110x110 m.
        assert self._ring_area(grown) == pytest.approx(110 * 110, rel=0.02)

    def test_buffered_ring_keeps_shoreline_elevation(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator

        square = [
            {"x": 1000.0, "y": 7.5, "z": 1000.0},
            {"x": 1100.0, "y": 7.5, "z": 1000.0},
            {"x": 1100.0, "y": 7.5, "z": 1100.0},
            {"x": 1000.0, "y": 7.5, "z": 1100.0},
        ]
        grown = EnfusionProjectGenerator._buffer_local_ring(square, 5.0)
        assert all(p["y"] == pytest.approx(7.5) for p in grown)

    def test_zero_buffer_is_a_no_op(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator

        square = [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 10.0, "y": 0.0, "z": 0.0},
            {"x": 10.0, "y": 0.0, "z": 10.0},
        ]
        assert EnfusionProjectGenerator._buffer_local_ring(square, 0.0) == square

    def test_degenerate_ring_is_returned_unchanged(self):
        """The margin is a nicety — never a reason to lose the lake."""
        from services.enfusion_project_generator import EnfusionProjectGenerator

        two_points = [{"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 1.0, "y": 0.0, "z": 0.0}]
        assert EnfusionProjectGenerator._buffer_local_ring(two_points, 5.0) == two_points

    def test_lake_layer_emits_a_buffered_spline(self, generator_factory):
        """End-to-end: the emitted lake spline must be larger than the source
        OSM polygon."""
        from config.lakes import LAKE_RING_BUFFER_M

        ring = [[1.0, 1.0], [1.1, 1.0], [1.1, 1.1], [1.0, 1.1], [1.0, 1.0]]
        water = {
            "type": "FeatureCollection",
            "features": [_polygon_feature([ring], water_type="lake", name="Testsjon")],
        }
        gen = generator_factory(water_features=water)
        out = gen._generate_water_layer()

        assert "SplineShapeEntity" in out
        assert LAKE_RING_BUFFER_M > 0
        # The identity transformer scales by 1000, so the source ring spans
        # x/z 1000..1100. A buffered ring must reach beyond that.
        xs = [
            float(line.split()[1])
            for line in out.splitlines()
            if line.strip().startswith("Position")
        ]
        assert xs, "no spline points emitted"


class TestLakeBathymetryConfig:
    """The shore slope — not the depth ceiling — is what made small lakes
    shallow: depth ramps linearly from the shore (#160)."""

    def test_lake_depth_and_slope_are_configurable(self):
        from config.lakes import LAKE_MAX_DEPTH_M, LAKE_SHORE_SLOPE_M_PER_M

        assert LAKE_MAX_DEPTH_M > 8.0, "old hardcoded ceiling was 8 m"
        assert LAKE_SHORE_SLOPE_M_PER_M > 0.3, "old hardcoded slope was 0.3 m/m"

    def test_small_lake_actually_gets_deep(self):
        """A 60 m-wide pond must reach a useful depth, not a puddle."""
        import numpy as np

        from config.lakes import LAKE_MAX_DEPTH_M, LAKE_SHORE_SLOPE_M_PER_M
        from services.heightmap_generator import flatten_water_in_heightmap

        elevation = np.full((80, 80), 100.0, dtype=np.float64)
        mask = np.zeros((80, 80), dtype=bool)
        mask[10:70, 10:70] = True  # 60 x 60 px at 1 m/px

        out = flatten_water_in_heightmap(
            elevation, mask,
            transition_px=3,
            pixel_size_m=1.0,
            max_depth_m=LAKE_MAX_DEPTH_M,
            shore_slope_m_per_m=LAKE_SHORE_SLOPE_M_PER_M,
        )
        deepest = float(np.min(out[mask]))
        carved = 100.0 - deepest
        assert carved > 8.0, (
            f"60 m pond only carved {carved:.1f} m below the surface"
        )


# ---------------------------------------------------------------------------
# Issue #170 — clipping before the union must not change what gets emitted
# ---------------------------------------------------------------------------

class TestClipBeforeUnionEquivalence:
    """
    The #170 fix discards out-of-map geometry before unioning instead of after
    projecting. That is only a legitimate optimisation if the emitted `.layer`
    text is identical, so pin exactly that: run each layer with clipping on and
    with it disabled, and diff the output.
    """

    @staticmethod
    def _without_clipping(gen):
        """Restore the pre-#170 behaviour: hand the cleanup no bounds."""
        gen._wgs84_bounds = lambda: None
        return gen

    def test_water_layer_is_byte_identical_with_and_without_the_pre_clip(
        self, generator_factory
    ):
        # A lake wholly inside, one straddling the terrain edge, and a river.
        water = {
            "type": "FeatureCollection",
            "features": [
                _polygon_feature(
                    [[[1.0, 1.0], [1.5, 1.0], [1.5, 1.5], [1.0, 1.5], [1.0, 1.0]]],
                    water_type="lake",
                    name="Inner",
                ),
                _polygon_feature(
                    [[[3.5, 3.5], [4.5, 3.5], [4.5, 4.5], [3.5, 4.5], [3.5, 3.5]]],
                    water_type="lake",
                    name="EdgeStraddler",
                ),
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.5, 0.5], [1.0, 1.2], [2.0, 2.0]],
                    },
                    "properties": {"water_type": "river"},
                },
            ],
        }

        clipped = generator_factory(water_features=copy.deepcopy(water))
        unclipped = self._without_clipping(
            generator_factory(water_features=copy.deepcopy(water))
        )

        assert clipped._generate_water_layer() == unclipped._generate_water_layer()

    def test_vegetation_layer_is_byte_identical_with_and_without_the_pre_clip(
        self, generator_factory
    ):
        forests = {
            "type": "FeatureCollection",
            "features": [
                _polygon_feature(
                    [[[0.5, 0.5], [1.2, 0.5], [1.2, 1.2], [0.5, 1.2], [0.5, 0.5]]],
                    landuse="forest",
                ),
                # Two overlapping woods that must still merge into one.
                _polygon_feature(
                    [[[2.0, 2.0], [2.6, 2.0], [2.6, 2.6], [2.0, 2.6], [2.0, 2.0]]],
                    natural="wood",
                ),
                _polygon_feature(
                    [[[2.3, 2.3], [2.9, 2.3], [2.9, 2.9], [2.3, 2.9], [2.3, 2.3]]],
                    natural="wood",
                ),
            ],
        }

        clipped = generator_factory(forest_features=copy.deepcopy(forests))
        unclipped = self._without_clipping(
            generator_factory(forest_features=copy.deepcopy(forests))
        )

        body = clipped._generate_vegetation_layer()
        assert body == unclipped._generate_vegetation_layer()
        assert "SplineShapeEntity" in body  # the comparison isn't vacuous

    def test_a_lake_far_larger_than_the_map_still_emits_its_in_map_spline(
        self, generator_factory
    ):
        """
        The reported case: OSM hands back an entire lake thousands of times the
        map area. The spline must still be emitted and must still cover the
        terrain — the fix removes work, not water.
        """
        giant = {
            "type": "FeatureCollection",
            "features": [
                _polygon_feature(
                    [[[-50.0, -50.0], [50.0, -50.0], [50.0, 50.0],
                      [-50.0, 50.0], [-50.0, -50.0]]],
                    water_type="lake",
                    name="Vänern",
                )
            ],
        }
        body = generator_factory(
            water_features=copy.deepcopy(giant)
        )._generate_water_layer()
        assert "SplineShapeEntity" in body

    def test_wgs84_bounds_prefers_the_transformer_over_the_metadata(
        self, generator_factory
    ):
        gen = generator_factory()
        # The identity transformer exposes no bounds, so the metadata bbox
        # (0..4°) is the base — then padded outward, never inward.
        from_metadata = gen._wgs84_bounds()
        assert from_metadata[0] < 0.0 and from_metadata[2] > 4.0

        class _BoundedTransformer(_IdentityTransformer):
            west, south, east, north = 14.0, 59.0, 14.05, 59.025

        gen.transformer = _BoundedTransformer()
        from_transformer = gen._wgs84_bounds()
        assert from_transformer[0] < 14.0 and from_transformer[2] > 14.05

    def test_bounds_always_cover_the_full_terrain_rectangle(
        self, generator_factory
    ):
        """
        The terrain grid is snapped to the nearest 128-face tile, so it can
        overhang the drawn bbox — here 4096 m of terrain over a 4000 m bbox.
        Clipping flush to the bbox would cut geometry the terrain still covers,
        so the returned rectangle must reach past the terrain's far corner.
        """
        gen = generator_factory()
        west, south, east, north = gen._wgs84_bounds()

        # Project the clip rectangle's NE corner into local metres and check it
        # clears the terrain, which spans 0..4096 m from the bbox SW corner.
        ne = gen.transformer.transform_points([{"x": east, "y": north}])[0]
        assert ne["x"] > gen.terrain_width
        assert ne["z"] > gen.terrain_depth

        sw = gen.transformer.transform_points([{"x": west, "y": south}])[0]
        assert sw["x"] < 0.0 and sw["z"] < 0.0

    def test_missing_bounds_everywhere_is_not_fatal(self, generator_factory):
        gen = generator_factory()
        gen.transformer = _IdentityTransformer()
        gen.metadata = dict(gen.metadata, input={})
        assert gen._wgs84_bounds() is None

    def test_a_transformer_that_blows_up_leaves_the_bounds_alone(
        self, generator_factory
    ):
        """A margin calculation must never be the reason a lake disappears."""
        gen = generator_factory()

        class _BrokenTransformer:
            def transform_points(self, points, elevation_array=None):
                raise RuntimeError("projection unavailable")

        gen.transformer = _BrokenTransformer()
        assert gen._wgs84_bounds() == (0.0, 0.0, 4.0, 4.0)
