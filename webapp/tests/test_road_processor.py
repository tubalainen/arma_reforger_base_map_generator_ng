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
        assert result == {"roads": [], "stats": {}}

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
