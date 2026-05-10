"""
Tests for the auto-attached RoadGeneratorEntity child in the roads layer
(Phase 1 / task A1).

Pre-Phase-1 the roads layer emitted SplineShapeEntity entries only and the
user had to right-click each one in Workbench to add a RoadGeneratorEntity
child. The generator now emits the child prefab inline using the same
``${guid}path/to/prefab.et { coords ... }`` syntax the managers layer uses,
backed by the validate_road_prefab safeguard so fabricated prefab names
never reach the .layer file.
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
        return EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            road_data={"roads": roads},
            transformer=_IdentityTransformer(),
            elevation_array=None,
        )

    return _make


class TestRoadsLayerAutoAttach:
    def test_road_emits_spline_with_prefab_child(self, make_generator):
        gen = make_generator([_road(0, "RG_Road_Asphalt_6m", name="E39")])
        out = gen._generate_roads_layer()

        # Spline parent entity exists with the road name as a comment.
        assert "SplineShapeEntity Road_0 {" in out
        assert "// E39" in out

        # The prefab child reference is nested inside the spline body.
        from config.enfusion import ARMA_REFORGER_GUID, ROAD_PREFAB_BASE
        expected_ref = (
            f"${{{ARMA_REFORGER_GUID}}}{ROAD_PREFAB_BASE}/RG_Road_Asphalt_6m.et"
        )
        assert expected_ref in out
        assert out.count(expected_ref) == 1

    def test_unknown_prefab_is_normalized_before_emit(self, make_generator):
        from config.roads import KNOWN_ROAD_PREFABS

        gen = make_generator([_road(0, "RG_Road_Asphalt_99m")])
        out = gen._generate_roads_layer()

        # The fabricated 99m name must NOT reach the layer file.
        assert "RG_Road_Asphalt_99m" not in out
        # Whatever prefab DID get emitted must be from the known-good set.
        emitted = [p for p in KNOWN_ROAD_PREFABS if f"/{p}.et" in out]
        assert len(emitted) == 1, (
            f"Expected exactly one known prefab in output; found {emitted}"
        )

    def test_each_spline_gets_exactly_one_child_prefab(self, make_generator):
        gen = make_generator([
            _road(0, "RG_Road_Asphalt_6m"),
            _road(1, "RG_Road_Gravel_4m"),
            _road(2, "RG_Road_Dirt_2m"),
        ])
        out = gen._generate_roads_layer()

        # 3 splines, 3 prefab children — one each.
        assert out.count("SplineShapeEntity Road_") == 3
        from config.enfusion import ROAD_PREFAB_BASE
        assert out.count(f"{ROAD_PREFAB_BASE}/") == 3

    def test_no_road_data_emits_friendly_comment(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator
        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            road_data=None,
            transformer=_IdentityTransformer(),
        )
        out = gen._generate_roads_layer()
        assert "SplineShapeEntity" not in out
        assert "no road data" in out.lower()

    def test_road_with_too_few_points_skipped(self, make_generator):
        bad = _road(0, "RG_Road_Asphalt_6m")
        bad["spline_points"] = [{"x": 1.0, "y": 1.0, "z": 0}]  # only 1 point
        gen = make_generator([bad])
        out = gen._generate_roads_layer()
        assert "SplineShapeEntity" not in out
