"""Tests for services/road_processor.py — road classification logic."""

from __future__ import annotations

import pytest


class TestInferRoadSurface:
    """Test road surface inference from OSM tags and country rules."""

    def test_explicit_asphalt_tag(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("track", "asphalt", "NO")
        assert result == "asphalt"

    def test_explicit_gravel_tag(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("primary", "gravel", "NO")
        assert result == "gravel"

    def test_explicit_unpaved_maps_to_gravel(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("track", "unpaved", "NO")
        assert result == "gravel"

    def test_motorway_always_asphalt(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("motorway", "", "NO")
        assert result == "asphalt"

    def test_primary_always_asphalt(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("primary", "", "SE")
        assert result == "asphalt"

    def test_track_norway_defaults_gravel(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("track", "", "NO")
        assert result == "gravel"

    def test_track_in_forest_estonia_defaults_dirt(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("track", "", "EE", is_in_forest=True)
        assert result == "dirt"

    def test_residential_urban_is_asphalt(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("residential", "", "NO", is_in_urban=True)
        assert result == "asphalt"

    def test_residential_rural_norway_is_gravel(self):
        from services.road_processor import infer_road_surface
        result = infer_road_surface("residential", "", "NO", is_in_urban=False)
        assert result == "gravel"

    def test_path_always_dirt(self):
        from services.road_processor import infer_road_surface
        for hw in ("path", "footway", "bridleway"):
            result = infer_road_surface(hw, "", "SE")
            assert result == "dirt", f"{hw} should be dirt"


class TestInferRoadWidth:
    """Test road width inference."""

    def test_explicit_width_tag(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("primary", "7.5m", "", "asphalt")
        assert result == 7.5

    def test_explicit_width_no_unit(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("primary", "8", "", "asphalt")
        assert result == 8.0

    def test_width_from_lanes(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("residential", "", "2", "asphalt")
        assert result == 7.0  # 2 * 3.5

    def test_width_from_lanes_gravel(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("track", "", "1", "gravel")
        assert result == 2.5  # 1 * 2.5

    def test_default_width_motorway(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("motorway", "", "", "asphalt")
        assert result == 14.0

    def test_default_width_track(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("track", "", "", "gravel")
        assert result == 3.0

    def test_default_width_unknown_type(self):
        from services.road_processor import infer_road_width
        result = infer_road_width("unknown_type", "", "", "asphalt")
        assert result == 4.0


class TestGetWidthClass:
    """Test width classification."""

    def test_wide(self):
        from services.road_processor import get_width_class
        assert get_width_class(14.0) == "wide"
        assert get_width_class(7.0) == "wide"

    def test_medium(self):
        from services.road_processor import get_width_class
        assert get_width_class(6.0) == "medium"
        assert get_width_class(4.0) == "medium"

    def test_narrow(self):
        from services.road_processor import get_width_class
        assert get_width_class(3.0) == "narrow"
        assert get_width_class(1.5) == "narrow"


class TestProcessRoads:
    """Test the full road processing pipeline."""

    def test_empty_input(self):
        from services.road_processor import process_roads
        result = process_roads(None, "NO")
        assert result["roads"] == []
        # Empty stats are zeroed out per category (added in v1.4.0 to keep
        # downstream consumers from needing key-presence checks).
        assert result["stats"]["total"] == 0

    def test_empty_features(self):
        from services.road_processor import process_roads
        result = process_roads({"type": "FeatureCollection", "features": []}, "NO")
        assert result["roads"] == []
        assert result["stats"]["total"] == 0

    def test_basic_processing(self, sample_road_features):
        from services.road_processor import process_roads
        result = process_roads(sample_road_features, "NO")
        assert result["stats"]["total"] == 2
        roads = result["roads"]
        # Primary road should be asphalt
        primary = [r for r in roads if r["highway_type"] == "primary"][0]
        assert primary["surface"] == "asphalt"
        assert primary["width_m"] == 7.0  # explicit width tag
        # Track should use Norway defaults
        track = [r for r in roads if r["highway_type"] == "track"][0]
        assert track["surface"] == "gravel"


class TestExportRoadsGeojson:
    """Test GeoJSON export."""

    def test_roundtrip(self, sample_road_features):
        from services.road_processor import process_roads, export_roads_geojson
        processed = process_roads(sample_road_features, "NO")
        geojson = export_roads_geojson(processed)
        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) == 2
        for f in geojson["features"]:
            assert f["geometry"]["type"] == "LineString"
            assert "enfusion_prefab" in f["properties"]


class TestExportRoadsSplineCsv:
    """Test CSV export."""

    def test_csv_header(self, sample_road_features):
        from services.road_processor import process_roads, export_roads_spline_csv
        processed = process_roads(sample_road_features, "NO")
        csv = export_roads_spline_csv(processed)
        lines = csv.strip().split("\n")
        assert lines[0].startswith("road_id,prefab,")
        assert len(lines) > 1


class TestValidateRoadPrefab:
    """v1.4.0 — Atlas 2 canonical names. validate_road_prefab snaps any
    fabricated `_<width>m` name to the closest canonical prefab on the same
    surface, and falls back to RG_Road_Asphalt_E_01_Narrow for anything we
    can't infer a surface for."""

    def test_atlas2_canonical_prefab_passes_through(self):
        from config.roads import validate_road_prefab
        assert validate_road_prefab("RG_Road_Asphalt_E_01") == "RG_Road_Asphalt_E_01"
        assert validate_road_prefab("RG_Road_Asphalt_E_03") == "RG_Road_Asphalt_E_03"
        assert validate_road_prefab("RG_TrailGravel_01") == "RG_TrailGravel_01"

    def test_legacy_width_suffix_snaps_to_asphalt_family(self):
        from config.roads import validate_road_prefab, KNOWN_ROAD_PREFABS
        # Legacy v1.3.x name → should land somewhere in the asphalt family.
        out = validate_road_prefab("RG_Road_Asphalt_5.5m")
        assert out in KNOWN_ROAD_PREFABS
        assert "Asphalt" in out

    def test_legacy_wide_asphalt_lands_in_asphalt_family(self):
        from config.roads import validate_road_prefab, KNOWN_ROAD_PREFABS
        # Legacy 11m asphalt → still asphalt, picker uses preference list.
        out = validate_road_prefab("RG_Road_Asphalt_11m")
        assert out in KNOWN_ROAD_PREFABS
        assert "Asphalt" in out

    def test_unknown_surface_falls_back_to_default(self):
        from config.roads import validate_road_prefab
        # Made-up "Mud" surface — no surface inference possible.
        assert validate_road_prefab("RG_Road_Mud_5m") == "RG_Road_Asphalt_E_01_Narrow"

    def test_garbage_input_falls_back_to_default(self):
        from config.roads import validate_road_prefab
        assert validate_road_prefab("totally bogus") == "RG_Road_Asphalt_E_01_Narrow"

    def test_process_roads_only_emits_known_prefabs(self, sample_road_features):
        from config.roads import KNOWN_ROAD_PREFABS
        from services.road_processor import process_roads
        processed = process_roads(sample_road_features, "NO")
        for road in processed["roads"]:
            assert road["enfusion_prefab"] in KNOWN_ROAD_PREFABS, (
                f"Fabricated prefab leaked through validator: "
                f"{road['enfusion_prefab']}"
            )
