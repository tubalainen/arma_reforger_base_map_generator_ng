"""Tests for services/feature_extractor.py — feature extraction logic."""

from __future__ import annotations

import pytest


class TestExtractWaterFeatures:
    """Test water feature extraction."""

    def test_empty_input(self):
        from services.feature_extractor import extract_water_features
        result = extract_water_features(None)
        assert result["lakes"] == []
        assert result["rivers"] == []

    def test_empty_features(self):
        from services.feature_extractor import extract_water_features
        result = extract_water_features({"features": []})
        assert result["stats"]["lakes"] == 0

    def test_lake_extraction(self, sample_water_features):
        from services.feature_extractor import extract_water_features
        result = extract_water_features(sample_water_features, "NO")
        assert result["stats"]["lakes"] == 1
        assert result["lakes"][0]["name"] == "Test Lake"
        assert result["lakes"][0]["type"] == "lake"

    def test_river_extraction(self, sample_water_features):
        from services.feature_extractor import extract_water_features
        result = extract_water_features(sample_water_features, "NO")
        assert result["stats"]["rivers"] == 1
        assert result["rivers"][0]["name"] == "Test River"
        assert result["rivers"][0]["width_estimate"] == 15.0


class TestEstimateRiverWidth:
    """Test river width estimation."""

    def test_river_width(self):
        from services.feature_extractor import _estimate_river_width
        assert _estimate_river_width("river") == 15.0
        assert _estimate_river_width("stream") == 3.0
        assert _estimate_river_width("canal") == 8.0
        assert _estimate_river_width("unknown") == 5.0


class TestExtractForestFeatures:
    """Test forest feature extraction."""

    def test_empty_input(self):
        from services.feature_extractor import extract_forest_features
        result = extract_forest_features(None)
        assert result["forests"] == []

    def test_coniferous_detection(self, sample_forest_features):
        from services.feature_extractor import extract_forest_features
        result = extract_forest_features(sample_forest_features, "NO")
        assert result["stats"]["total_areas"] == 1
        forest = result["forests"][0]
        assert forest["forest_type"] == "coniferous"
        assert forest["density"] == 0.7

    def test_country_default_type(self):
        from services.feature_extractor import extract_forest_features
        # Feature without leaf_type should use country default
        features = {
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[10, 60], [11, 60], [11, 61], [10, 61], [10, 60]]
                        ],
                    },
                    "properties": {"osm_id": 1, "type": "wood"},
                },
            ],
        }
        result_dk = extract_forest_features(features, "DK")
        assert result_dk["forests"][0]["forest_type"] == "deciduous"

        result_ee = extract_forest_features(features, "EE")
        assert result_ee["forests"][0]["forest_type"] == "mixed"


class TestGetSpeciesForType:
    """Test tree species mapping."""

    def test_known_combo(self):
        from services.feature_extractor import _get_species_for_type
        species = _get_species_for_type("coniferous", "NO")
        assert "spruce" in species
        assert "pine" in species

    def test_unknown_country_uses_default(self):
        from services.feature_extractor import _get_species_for_type
        species = _get_species_for_type("coniferous", "XX")
        assert species == ["spruce", "pine"]

    def test_unknown_type_uses_generic(self):
        from services.feature_extractor import _get_species_for_type
        species = _get_species_for_type("unknown_type", "NO")
        assert species == ["generic_tree"]


class TestExtractBuildingFeatures:
    """Test building feature extraction."""

    def test_empty_input(self):
        from services.feature_extractor import extract_building_features
        result = extract_building_features(None)
        assert result["buildings"] == []

    def test_basic_building(self, sample_building_features):
        from services.feature_extractor import extract_building_features
        result = extract_building_features(sample_building_features, "NO")
        assert result["stats"]["total"] == 1
        building = result["buildings"][0]
        assert building["building_type"] == "house"
        assert building["height_m"] == 6
        assert building["prefab_category"] == "Building_House"


class TestEstimateBuildingRotation:
    """Test building rotation estimation."""

    def test_axis_aligned_building(self):
        from services.feature_extractor import _estimate_building_rotation
        geom = {
            "coordinates": [
                [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
            ],
        }
        rotation = _estimate_building_rotation(geom)
        assert rotation == 0.0  # Longest edge along x-axis

    def test_short_polygon(self):
        from services.feature_extractor import _estimate_building_rotation
        geom = {"coordinates": [[[0, 0], [1, 0]]]}
        assert _estimate_building_rotation(geom) == 0


class TestExtractAllFeatures:
    """Test the full feature extraction pipeline."""

    def test_combines_all_types(self, sample_water_features, sample_forest_features, sample_building_features):
        from services.feature_extractor import extract_all_features
        osm_data = {
            "water": sample_water_features,
            "forests": sample_forest_features,
            "buildings": sample_building_features,
        }
        result = extract_all_features(osm_data, "NO")
        assert result["summary"]["lakes"] == 1
        assert result["summary"]["rivers"] == 1
        assert result["summary"]["forest_areas"] == 1
        assert result["summary"]["buildings"] == 1
