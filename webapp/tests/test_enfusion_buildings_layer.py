"""
Tests for the Phase 2 buildings layer (audit task A2 + L12).

The buildings layer emits one entity per OSM building, in one of two modes:

* **Auto-placed prefab instance** when ``config.buildings.KNOWN_BUILDING_PREFABS``
  has a verified path for the building's category. Uses the
  ``${guid}path/to/prefab.et { coords X Y Z }`` syntax.
* **Footprint-spline marker** when the catalog has no entry — emits a closed
  ``SplineShapeEntity`` over the exterior ring so the user can manually wire
  a building prefab as a child entity.

Buildings whose centroid falls inside an asphalt-road exclusion zone (half
road width + 1.5 m safety) are dropped (L12).
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


def _building(idx, lon, lat, building_type="house", prefab=None, name=""):
    """Synthetic building dict matching extract_building_features output."""
    # Tiny 0.0001-degree square footprint around (lon, lat)
    d = 0.0001
    return {
        "osm_id": 4000 + idx,
        "name": name,
        "building_type": building_type,
        "height_m": 6,
        "center": [lon, lat],
        "rotation_deg": 0,
        "footprint_area_m2": 100,
        "prefab_category": f"Building_{building_type.capitalize()}",
        "enfusion_prefab": prefab,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon - d, lat - d],
                [lon + d, lat - d],
                [lon + d, lat + d],
                [lon - d, lat + d],
                [lon - d, lat - d],
            ]],
        },
    }


def _asphalt_road(idx, lon_start, lat_start, lon_end, lat_end, width_m=6):
    return {
        "osm_id": 1000 + idx,
        "name": f"Road_{idx}",
        "highway_type": "primary",
        "surface": "asphalt",
        "width_m": width_m,
        "is_bridge": False,
        "is_tunnel": False,
        "enfusion_prefab": "RG_Road_Asphalt_6m",
        "spline_points": [
            {"x": lon_start, "y": lat_start, "z": 0},
            {"x": lon_end, "y": lat_end, "z": 0},
        ],
        "point_count": 2,
    }


@pytest.fixture
def make_generator():
    from services.enfusion_project_generator import EnfusionProjectGenerator

    _SENTINEL = object()

    def _make(buildings=_SENTINEL, roads=None):
        if buildings is _SENTINEL:
            building_data = None
        elif buildings is None:
            building_data = None
        else:
            building_data = {"buildings": list(buildings)}
        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km_terrain(),
            road_data={"roads": roads or []},
            transformer=_IdentityTransformer(),
            elevation_array=None,
            building_data=building_data,
        )
        gen._reset_naming_state()
        return gen
    return _make


def _shapely_available() -> bool:
    try:
        import shapely  # noqa: F401
        from shapely.geometry import LineString  # noqa: F401
        return True
    except ImportError:
        return False


requires_shapely = pytest.mark.skipif(
    not _shapely_available(),
    reason="shapely not installed in this environment — L12 path is skipped at runtime too",
)


# ---------------------------------------------------------------------------
# validate_building_prefab — Phase 2 honest "we don't fabricate paths" guard
# ---------------------------------------------------------------------------

class TestValidateBuildingPrefab:
    def test_unknown_category_returns_none(self):
        from config.buildings import validate_building_prefab
        # An unmapped category (e.g. a hypothetical future "Building_Lighthouse")
        # falls back to footprint-marker mode by returning None. Categories
        # currently in the catalog all resolve — see test_known_category_resolves.
        assert validate_building_prefab("Building_DoesNotExist_42") is None

    def test_none_input_returns_none(self):
        from config.buildings import validate_building_prefab
        assert validate_building_prefab(None) is None
        assert validate_building_prefab("") is None

    def test_known_category_resolves_to_atlas_verified_path(self):
        """v1.4.1 — KNOWN_BUILDING_PREFABS is populated. Every category the
        feature_extractor produces must now resolve to a Prefabs/Structures
        path that has been verified against community mod source."""
        from config.buildings import validate_building_prefab
        path = validate_building_prefab("Building_House")
        assert path is not None
        assert path.startswith("Prefabs/Structures/")
        assert path.endswith(".et")

    def test_catalog_covers_every_feature_extractor_category(self):
        """Regression guard: if feature_extractor adds a new category label
        without a matching catalogue entry, every building of that type would
        silently fall back to footprint markers. Pin the contract."""
        from config.buildings import KNOWN_BUILDING_PREFABS
        # These are the exact category labels produced by
        # services.feature_extractor.extract_building_features().
        expected_categories = {
            "Building_House",
            "Building_Residential",
            "Building_Apartments",
            "Building_Church",
            "Building_Commercial",
            "Building_Industrial",
            "Building_Garage",
            "Building_Barn",
            "Building_Shed",
            "Building_Generic",
        }
        missing = expected_categories - KNOWN_BUILDING_PREFABS.keys()
        assert not missing, (
            f"feature_extractor emits categor(y/ies) {missing} that aren't in "
            f"KNOWN_BUILDING_PREFABS — buildings of that type will fall back "
            f"to footprint markers."
        )


# ---------------------------------------------------------------------------
# Buildings layer — emission modes
# ---------------------------------------------------------------------------

class TestBuildingsLayerEmission:
    def test_no_building_data_emits_friendly_comment(self, make_generator):
        # building_data=None (not even an empty dict) → distinct message.
        gen = make_generator(buildings=None)
        out = gen._generate_buildings_layer()
        assert "// Buildings layer" in out
        assert "No building data available" in out
        assert "SplineShapeEntity" not in out

    def test_empty_building_list_emits_friendly_comment(self, make_generator):
        gen = make_generator(buildings=[])
        out = gen._generate_buildings_layer()
        assert "No buildings found" in out
        assert "SplineShapeEntity" not in out

    def test_unvalidated_building_emits_footprint_spline(self, make_generator):
        gen = make_generator(buildings=[
            _building(0, lon=1.0, lat=1.0, building_type="house", prefab=None,
                      name="Test House")
        ])
        out = gen._generate_buildings_layer()

        # v1.4.0 — footprint markers now use descriptive names derived from OSM
        # tags (Building_House_TestHouse), no longer Building_<index>.
        assert "SplineShapeEntity Building_House_TestHouse {" in out
        # Comment carries the human-readable name + type
        assert "Test House" in out
        # No actual ${guid}prefab.et reference was emitted for this building.
        from config.enfusion import ARMA_REFORGER_GUID
        assert f"${{{ARMA_REFORGER_GUID}}}" not in out

    def test_validated_building_emits_prefab_instance(self, make_generator):
        from config.enfusion import ARMA_REFORGER_GUID
        b = _building(0, lon=1.0, lat=1.0, building_type="house",
                      prefab="Prefabs/Structures/Civilian/Test_House.et")
        gen = make_generator(buildings=[b])
        out = gen._generate_buildings_layer()

        expected_ref = (
            f"${{{ARMA_REFORGER_GUID}}}"
            "Prefabs/Structures/Civilian/Test_House.et"
        )
        assert expected_ref in out
        # Prefab-instance mode does NOT emit a SplineShapeEntity at all
        # for this building (no Building_* spline header).
        assert "SplineShapeEntity Building_" not in out

    def test_building_outside_terrain_skipped(self, make_generator):
        # Identity ×1000: lon=10 → x=10000m, terrain only 4096m wide.
        gen = make_generator(buildings=[
            _building(0, lon=10.0, lat=10.0, building_type="house"),
        ])
        out = gen._generate_buildings_layer()
        # The header text mentions `Building_*.et` — strip that before
        # checking that no real entity was emitted.
        body = out.split("// Source data:")[-1]
        assert "SplineShapeEntity" not in body
        # Counter is empty: no entity carrying a Building_<something> name.
        assert "SplineShapeEntity Building_" not in out

    def test_each_building_gets_one_entity(self, make_generator):
        gen = make_generator(buildings=[
            _building(0, lon=0.5, lat=0.5),
            _building(1, lon=1.0, lat=1.0),
            _building(2, lon=1.5, lat=1.5),
        ])
        out = gen._generate_buildings_layer()
        # Three footprint markers (none have validated prefabs). Names are now
        # descriptive (Building_<category>_<quadrant>_NNN) rather than
        # Building_<idx> — count by the SplineShapeEntity Building_ prefix.
        assert out.count("SplineShapeEntity Building_") == 3


# ---------------------------------------------------------------------------
# L12 — buildings overlapping asphalt roads are dropped
# ---------------------------------------------------------------------------

@requires_shapely
class TestL12RoadDeconflict:
    def test_building_on_asphalt_road_is_dropped(self, make_generator):
        # Asphalt road running along latitude=1.0 from lon=0.5 to lon=2.0
        road = _asphalt_road(0, 0.5, 1.0, 2.0, 1.0, width_m=8)
        # Building centroid sits ON the road — should be dropped.
        on_road = _building(0, lon=1.0, lat=1.0, name="OnRoad")
        # Building 100 m off the road — should survive.
        off_road = _building(1, lon=1.0, lat=1.01, name="OffRoad")

        gen = make_generator(buildings=[on_road, off_road], roads=[road])
        out = gen._generate_buildings_layer()

        assert "OnRoad" not in out
        assert "OffRoad" in out

    def test_non_asphalt_road_does_not_exclude_buildings(self, make_generator):
        # Gravel roads should NOT trigger L12 — the audit only excludes
        # buildings on paved roads (gravel/dirt tracks can have farmhouses
        # right next to them).
        gravel = _asphalt_road(0, 0.5, 1.0, 2.0, 1.0)
        gravel["surface"] = "gravel"
        on_gravel = _building(0, lon=1.0, lat=1.0, name="OnGravel")

        gen = make_generator(buildings=[on_gravel], roads=[gravel])
        out = gen._generate_buildings_layer()
        assert "OnGravel" in out

    def test_no_road_data_skips_l12_check(self, make_generator):
        b = _building(0, lon=1.0, lat=1.0, name="Standalone")
        gen = make_generator(buildings=[b], roads=None)
        out = gen._generate_buildings_layer()
        assert "Standalone" in out


# ---------------------------------------------------------------------------
# Integration — buildings.layer is part of generate_all + world.ent index
# ---------------------------------------------------------------------------

class TestBuildingsLayerWiring:
    def test_world_ent_includes_buildings_layer(self, make_generator):
        gen = make_generator()
        ent = gen._generate_world_ent()
        assert "Layer buildings {" in ent

    def test_generate_all_writes_buildings_layer(self, tmp_path, make_generator):
        gen = make_generator(buildings=[_building(0, lon=1.0, lat=1.0)])
        files = gen.generate_all(tmp_path)
        assert "buildings.layer" in files
        layer_path = Path(files["buildings.layer"])
        assert layer_path.exists()
        content = layer_path.read_text(encoding="utf-8")
        assert "Buildings layer" in content
