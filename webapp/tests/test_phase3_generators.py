"""
Tests for Phase 3 features:
  A3 — ForestGeneratorEntity child auto-attached to forest splines
  A4 — LakeGeneratorEntity child auto-attached to lake splines
  A5 — River/stream LineStrings emitted as open splines in water.layer
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


# ---------------------------------------------------------------------------
# Shared helpers (duplicate-free: same identity transformer as existing tests)
# ---------------------------------------------------------------------------

class _IdentityTransformer:
    def transform_points(self, points, elevation_array=None):
        return [
            {"x": round(p["x"] * 1000, 3), "y": 0.0, "z": round(p["y"] * 1000, 3)}
            for p in points
        ]


def _metadata():
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


def _poly_feature(coords, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": props,
    }


def _line_feature(coords, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def _make_gen(**kwargs):
    from services.enfusion_project_generator import EnfusionProjectGenerator
    return EnfusionProjectGenerator(
        map_name="TestMap",
        metadata=_metadata(),
        transformer=_IdentityTransformer(),
        elevation_array=None,
        **kwargs,
    )


_RING = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]
_RIVER_LINE = [[0.5, 0.5], [1.0, 0.5], [1.5, 0.5], [2.0, 0.5]]
_ARMA_GUID = "58D0FB3206B6F859"


# ---------------------------------------------------------------------------
# A3 — Forest generator child
# ---------------------------------------------------------------------------

class TestForestGeneratorChild:
    def test_empty_catalog_emits_spline_only(self):
        """Default behavior: catalog empty → plain spline, no child block."""
        gen = _make_gen(forest_features={
            "type": "FeatureCollection",
            "features": [_poly_feature([_RING], leaf_type="needleleaved")],
        })
        out = gen._generate_vegetation_layer()
        assert "SplineShapeEntity ForestArea_0 {" in out
        assert f"${{{_ARMA_GUID}}}" not in out

    def test_populated_catalog_emits_child_prefab(self):
        """When catalog has an entry, the child block appears in the spline."""
        fake_prefab = "Prefabs/WEGenerators/Forest/FG_PineForest_01.et"
        with patch("config.forests.KNOWN_FOREST_PREFABS", {"coniferous": fake_prefab}):
            gen = _make_gen(forest_features={
                "type": "FeatureCollection",
                "features": [_poly_feature([_RING], leaf_type="needleleaved")],
            })
            out = gen._generate_vegetation_layer()

        assert "SplineShapeEntity ForestArea_0 {" in out
        assert f"${{{_ARMA_GUID}}}{fake_prefab}" in out
        assert "coords 0 0 0" in out

    def test_unrecognised_leaf_type_falls_back_to_mixed_key(self):
        """Unknown leaf_type → forest_type_from_osm returns 'mixed'."""
        from config.forests import forest_type_from_osm
        assert forest_type_from_osm({"leaf_type": "exotic"}) == "mixed"

    def test_needleleaved_maps_to_coniferous(self):
        from config.forests import forest_type_from_osm
        assert forest_type_from_osm({"leaf_type": "needleleaved"}) == "coniferous"

    def test_broadleaved_maps_to_deciduous(self):
        from config.forests import forest_type_from_osm
        assert forest_type_from_osm({"leaf_type": "broadleaved"}) == "deciduous"

    def test_scrub_type_maps_to_scrub(self):
        from config.forests import forest_type_from_osm
        assert forest_type_from_osm({"type": "scrub"}) == "scrub"

    def test_validate_forest_prefab_none_for_empty_catalog(self):
        from config.forests import validate_forest_prefab
        assert validate_forest_prefab("coniferous") is None

    def test_validate_forest_prefab_returns_path_when_configured(self):
        fake = "Prefabs/WEGenerators/Forest/FG_PineForest_01.et"
        with patch("config.forests.KNOWN_FOREST_PREFABS", {"coniferous": fake}):
            from config.forests import validate_forest_prefab
            assert validate_forest_prefab("coniferous") == fake

    def test_mixed_forest_types_only_attach_when_catalog_has_matching_entry(self):
        """Coniferous polygon gets child; deciduous polygon stays spline-only."""
        pine_prefab = "Prefabs/WEGenerators/Forest/FG_PineForest_01.et"
        with patch("config.forests.KNOWN_FOREST_PREFABS", {"coniferous": pine_prefab}):
            gen = _make_gen(forest_features={
                "type": "FeatureCollection",
                "features": [
                    _poly_feature([_RING], leaf_type="needleleaved"),
                    _poly_feature(
                        [[[2.0, 2.0], [2.5, 2.0], [2.5, 2.5], [2.0, 2.5], [2.0, 2.0]]],
                        leaf_type="broadleaved",
                    ),
                ],
            })
            out = gen._generate_vegetation_layer()

        assert f"${{{_ARMA_GUID}}}{pine_prefab}" in out
        assert out.count(f"${{{_ARMA_GUID}}}") == 1  # only the coniferous one


# ---------------------------------------------------------------------------
# A4 — Lake generator child
# ---------------------------------------------------------------------------

class TestLakeGeneratorChild:
    def test_empty_catalog_emits_spline_only(self):
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_poly_feature([_RING], water_type="lake")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity Water_0 {" in out
        assert f"${{{_ARMA_GUID}}}" not in out

    def test_populated_catalog_emits_child_prefab(self):
        fake_prefab = "Prefabs/WEGenerators/Water/Lake/LG_Lake_01.et"
        with patch("config.lakes.KNOWN_LAKE_PREFABS", {"lake": fake_prefab}):
            gen = _make_gen(water_features={
                "type": "FeatureCollection",
                "features": [_poly_feature([_RING], water_type="lake")],
            })
            out = gen._generate_water_layer()

        assert "SplineShapeEntity Water_0 {" in out
        assert f"${{{_ARMA_GUID}}}{fake_prefab}" in out
        assert "coords 0 0 0" in out

    def test_pond_and_reservoir_both_get_child_when_catalogued(self):
        lake_pf = "Prefabs/WEGenerators/Water/Lake/LG_Lake_01.et"
        pond_pf = "Prefabs/WEGenerators/Water/Lake/LG_Lake_Small_01.et"
        catalog = {"lake": lake_pf, "pond": pond_pf}
        pond_ring = [[2.0, 2.0], [2.5, 2.0], [2.5, 2.5], [2.0, 2.5], [2.0, 2.0]]
        with patch("config.lakes.KNOWN_LAKE_PREFABS", catalog):
            gen = _make_gen(water_features={
                "type": "FeatureCollection",
                "features": [
                    _poly_feature([_RING], water_type="lake"),
                    _poly_feature([pond_ring], water_type="pond"),
                ],
            })
            out = gen._generate_water_layer()

        assert f"${{{_ARMA_GUID}}}{lake_pf}" in out
        assert f"${{{_ARMA_GUID}}}{pond_pf}" in out

    def test_validate_lake_prefab_none_for_empty_catalog(self):
        from config.lakes import validate_lake_prefab
        assert validate_lake_prefab("lake") is None

    def test_validate_lake_prefab_returns_path_when_configured(self):
        fake = "Prefabs/WEGenerators/Water/Lake/LG_Lake_01.et"
        with patch("config.lakes.KNOWN_LAKE_PREFABS", {"lake": fake}):
            from config.lakes import validate_lake_prefab
            assert validate_lake_prefab("lake") == fake

    def test_river_polygon_still_filtered_out(self):
        """water_type=river polygon must not get a lake generator child."""
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_poly_feature([_RING], water_type="river")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity" not in out.split("// River")[0]


# ---------------------------------------------------------------------------
# A5 — River / stream open splines
# ---------------------------------------------------------------------------

class TestRiverSplines:
    def test_river_linestring_emits_open_spline(self):
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature(_RIVER_LINE, water_type="river", name="Test River")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity River_0 {" in out
        assert "Test River" in out
        assert "~15m" in out  # river default width

    def test_stream_uses_3m_width_estimate(self):
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature(_RIVER_LINE, water_type="stream")],
        })
        out = gen._generate_water_layer()
        assert "~3m" in out

    def test_canal_uses_8m_width_estimate(self):
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature(_RIVER_LINE, water_type="canal")],
        })
        out = gen._generate_water_layer()
        assert "~8m" in out

    def test_river_spline_has_no_closing_repeat(self):
        """Open spline: first and last ShapePoint positions must differ."""
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature(_RIVER_LINE, water_type="river")],
        })
        out = gen._generate_water_layer()
        # The first point is always Position 0 0 0. If the spline were closed,
        # there would be at least 2 occurrences of "Position 0.000 0.000 0.000".
        # For an open spline with a moving river, the last point differs → only 1.
        assert out.count("Position 0.000 0.000 0.000") == 1

    def test_river_and_lake_coexist_in_water_layer(self):
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [
                _poly_feature([_RING], water_type="lake"),
                _line_feature(_RIVER_LINE, water_type="river", name="Ör River"),
            ],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity Water_0 {" in out
        assert "SplineShapeEntity River_0 {" in out
        assert "Ör River" in out

    def test_non_water_linestring_not_emitted(self):
        """Only river/stream/canal LineStrings are emitted."""
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature(_RIVER_LINE, water_type="coastline")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity River_" not in out

    def test_river_out_of_bounds_is_skipped(self):
        far_line = [[10.0, 10.0], [11.0, 10.0], [12.0, 10.0]]
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature(far_line, water_type="river")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity River_" not in out

    def test_degenerate_single_point_river_skipped(self):
        gen = _make_gen(water_features={
            "type": "FeatureCollection",
            "features": [_line_feature([[1.0, 1.0]], water_type="river")],
        })
        out = gen._generate_water_layer()
        assert "SplineShapeEntity River_" not in out

    def test_no_water_features_river_section_absent(self):
        gen = _make_gen(water_features=None)
        out = gen._generate_water_layer()
        assert "River_" not in out
        assert "// River / stream" not in out


# ---------------------------------------------------------------------------
# Setup guide — §6.1 / §6.2 updated text
# ---------------------------------------------------------------------------

class TestSetupGuidePhase3:
    def _make_guide(self):
        from services.setup_guide_generator import SetupGuideGenerator
        metadata = {
            **_metadata(),
            "surface_masks": {"coverage": {"per_surface": {}}},
            "roads": {},
            "features": {"lakes": 3, "rivers": 2, "forest_areas": 5, "buildings": 10},
            "satellite": {},
            "coordinate_transform": {},
            "enfusion_import": {"recommended_settings": {}},
        }
        return SetupGuideGenerator("TestMap", metadata)

    def test_forest_auto_attach_instructions_present(self):
        guide = self._make_guide()
        out = guide._phase_vegetation_water()
        assert "KNOWN_FOREST_PREFABS" in out
        assert "config/forests.py" in out

    def test_lake_auto_attach_instructions_present(self):
        guide = self._make_guide()
        out = guide._phase_vegetation_water()
        assert "KNOWN_LAKE_PREFABS" in out
        assert "config/lakes.py" in out

    def test_river_open_splines_mentioned(self):
        guide = self._make_guide()
        out = guide._phase_vegetation_water()
        assert "open" in out.lower()
        assert "river" in out.lower()

    def test_feature_counts_interpolated(self):
        guide = self._make_guide()
        out = guide._phase_vegetation_water()
        assert "**5** forest" in out
        assert "**3** lake" in out
        assert "**2** river" in out
