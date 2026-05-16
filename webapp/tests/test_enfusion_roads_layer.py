"""
Tests for the spline-only roads layer.

v1.1.0 (Phase 1 / task A1) tried to auto-attach a ``RoadGeneratorEntity``
child by nesting a ``${guid}path/to/prefab.et { coords ... }`` line inside
each ``SplineShapeEntity`` body. That nesting form is unsupported and hung
the World Editor at 4% on world load. v1.2.3 reverts to spline-only road
entities, with the validated prefab name surfaced in the spline's ``//``
comment so the user can attach the generator manually.

These tests pin the spline-only behaviour and the new
``_simplify_local_polyline`` vertex cap that protects the loader from
multi-thousand-point OSM ways.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


class _IdentityTransformer:
    """Multiplies WGS84 lon/lat by 1000 to get terrain-local x/z metres."""

    def transform_points(self, points, elevation_array=None):
        return [
            {"x": round(p["x"] * 1000, 3), "y": 0.0, "z": round(p["y"] * 1000, 3)}
            for p in points
        ]


def _metadata_for_4km_terrain() -> dict:
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


def _road(idx: int, prefab: str, name: str = "") -> dict:
    """Build a synthetic road entry resembling road_processor.process_roads output."""
    return {
        "osm_id": 1000 + idx,
        "name": name,
        "highway_type": "primary",
        "surface": "asphalt",
        "width_m": 6.0,
        "is_bridge": False,
        "is_tunnel": False,
        "enfusion_prefab": prefab,
        "spline_points": [
            {"x": 1.0, "y": 1.0, "z": 0},
            {"x": 1.5, "y": 1.5, "z": 0},
            {"x": 2.0, "y": 2.0, "z": 0},
        ],
        "point_count": 3,
    }


@pytest.fixture
def make_generator():
    """Build an EnfusionProjectGenerator with the identity transformer."""
    from services.enfusion_project_generator import EnfusionProjectGenerator

    def _make(roads):
        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            road_data={"roads": roads},
            transformer=_IdentityTransformer(),
            elevation_array=None,
        )
        # In production the namer is initialised inside generate_all; tests
        # call _generate_roads_layer directly, so seed the state manually.
        gen._reset_naming_state()
        return gen

    return _make


class TestRoadsLayerSplineOnly:
    def test_road_emits_spline_with_prefab_in_comment(self, make_generator):
        gen = make_generator([_road(0, "RG_Road_Asphalt_E_01", name="E39")])
        out = gen._generate_roads_layer()

        # v1.4.0 — entities are now named from the OSM ref/name.
        assert "SplineShapeEntity Road_E39_Asphalt {" in out
        # Comment surfaces the road name, prefab name, paints, and the
        # fully-qualified `{guid}path.et` form (the latter sourced from
        # Atlas 2's SCR_SHPPrefabDataList, p. 12).
        assert "// E39" in out
        assert "prefab: RG_Road_Asphalt_E_01" in out
        assert "paints: asphalt" in out
        assert "fq: {02AF8C5A31EC3A53}PrefabLibrary/Generators/Roads/Asphalt/RG_Road_Asphalt_E_01.et" in out

    def test_unknown_prefab_is_normalized_in_comment(self, make_generator):
        from config.roads import KNOWN_ROAD_PREFABS

        # Legacy fabricated name from v1.3.x — must be snapped to a canonical
        # Atlas 2 prefab before reaching the .layer file.
        gen = make_generator([_road(0, "RG_Road_Asphalt_99m")])
        out = gen._generate_roads_layer()

        assert "RG_Road_Asphalt_99m" not in out
        matched = [p for p in KNOWN_ROAD_PREFABS if f"prefab: {p}" in out]
        assert len(matched) == 1, (
            f"Expected exactly one known prefab in comment; found {matched}"
        )

    def test_no_nested_guid_reference_inside_splines(self, make_generator):
        """
        Regression guard for the v1.1.0 hang: no ``${...}`` instance line
        may appear inside any ``SplineShapeEntity Road_*`` body.
        """
        gen = make_generator([
            _road(0, "RG_Road_Asphalt_E_01"),
            _road(1, "RG_TrailGravel_01"),
            _road(2, "RG_TrailDirt_01"),
        ])
        out = gen._generate_roads_layer()

        assert out.count("SplineShapeEntity Road_") == 3
        # Workbench rejects nested ${guid}path.et inside SplineShapeEntity —
        # the .layer must contain zero such tokens.
        assert "${" not in out

    def test_no_road_data_emits_friendly_comment(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator
        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            road_data=None,
            transformer=_IdentityTransformer(),
        )
        # Roads layer is generated as part of generate_all; reset the namer
        # state manually for the standalone-call path the test uses.
        gen._reset_naming_state()
        out = gen._generate_roads_layer()
        assert "SplineShapeEntity" not in out
        assert "no road data" in out.lower()

    def test_road_with_too_few_points_skipped(self, make_generator):
        bad = _road(0, "RG_Road_Asphalt_E_01")
        bad["spline_points"] = [{"x": 1.0, "y": 1.0, "z": 0}]  # only 1 point
        gen = make_generator([bad])
        out = gen._generate_roads_layer()
        assert "SplineShapeEntity" not in out


class TestRoadsLayerVertexCap:
    """A long OSM way must be simplified to <= MAX_SPLINE_POINTS vertices
    so the World Editor doesn't choke on 'Loading entity data...'."""

    def test_long_road_capped_to_max_spline_points(self, make_generator):
        from config.enfusion import MAX_SPLINE_POINTS

        # 3000 collinear points across a 4 km terrain — RDP collapses easy.
        n = 3000
        long_road = _road(0, "RG_Road_Asphalt_E_01", name="LongRoad")
        long_road["spline_points"] = [
            {"x": 0.001 + (i / (n - 1)) * 3.998, "y": 1.0, "z": 0}
            for i in range(n)
        ]
        long_road["point_count"] = n

        gen = make_generator([long_road])
        out = gen._generate_roads_layer()

        sp_count = out.count("ShapePoint sp_")
        assert sp_count <= MAX_SPLINE_POINTS, (
            f"Road spline emitted {sp_count} points; "
            f"expected <= MAX_SPLINE_POINTS ({MAX_SPLINE_POINTS})"
        )
        assert sp_count >= 2

    def test_short_road_passes_through_unchanged(self, make_generator):
        # v1.4.4 — simplification now ALWAYS runs (previously gated on
        # len > MAX_SPLINE_POINTS), so collinear points get collapsed.
        # The default fixture has 3 collinear points → use a road with a
        # genuine corner to confirm meaningful geometry survives.
        road = _road(0, "RG_Road_Asphalt_E_01")
        road["spline_points"] = [
            {"x": 1.0, "y": 1.0, "z": 0},
            {"x": 2.0, "y": 1.0, "z": 0},  # corner — 90° turn
            {"x": 2.0, "y": 2.0, "z": 0},
        ]
        road["point_count"] = 3
        gen = make_generator([road])
        out = gen._generate_roads_layer()
        # All 3 points survive because the middle vertex is a true corner,
        # not a collinear filler. Confirms simplification doesn't over-cull.
        assert out.count("ShapePoint sp_") == 3
