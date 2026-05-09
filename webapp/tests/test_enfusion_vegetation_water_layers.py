"""
Tests for the auto-emitted vegetation.layer and water.layer in
EnfusionProjectGenerator (added in v1.0.6).

Pre-v1.0.6 these layers were single-comment placeholders. Now they emit one
closed `SplineShapeEntity` per forest / lake polygon.
"""

from __future__ import annotations

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
        return EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            transformer=_IdentityTransformer(),
            elevation_array=None,
            forest_features=forest_features,
            water_features=water_features,
        )
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

        # One entity with the expected name prefix
        assert out.count("SplineShapeEntity ForestArea_0 {") == 1
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
        assert "SplineShapeEntity ForestArea_0 {" in out
        assert "SplineShapeEntity ForestArea_1 {" in out

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
        assert out.count("SplineShapeEntity ForestArea_") == 1
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
        assert "SplineShapeEntity Water_0 {" in out

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
        assert "SplineShapeEntity Water_0 {" in out
        assert "SplineShapeEntity Water_1 {" in out
        assert "SplineShapeEntity Water_2 {" not in out

    def test_no_water_features_emits_empty_message(self, generator_factory):
        gen = generator_factory(water_features=None)
        out = gen._generate_water_layer()
        assert "// Water layer" in out
        assert "// No standing-water polygons" in out
        assert "SplineShapeEntity" not in out
