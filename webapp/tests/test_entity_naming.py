"""
Tests for the OSM-aware EntityNamer added in v1.4.0 (Atlas 2 alignment).

The namer replaces sequential IDs (Road_0, ForestArea_3, ...) with
descriptive names derived from OSM tags, falling back to a
quadrant + category scheme for anonymous features.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from services.entity_naming import EntityNamer, expected_surface, sanitize_token


@pytest.fixture
def namer():
    return EntityNamer(terrain_width=4096.0, terrain_depth=4096.0)


class TestSanitizeToken:
    def test_ascii_passthrough(self):
        assert sanitize_token("Storgatan") == "Storgatan"

    def test_swedish_vowel_folding(self):
        assert sanitize_token("Vänern") == "Vanern"
        assert sanitize_token("Älven") == "Alven"
        assert sanitize_token("Östersjön") == "Ostersjon"

    def test_norwegian_vowels(self):
        assert sanitize_token("Ørsted") == "Orsted"
        assert sanitize_token("Åland") == "Aland"

    def test_multiword_titlecased(self):
        assert sanitize_token("St Mary") == "StMary"
        assert sanitize_token("new york city") == "NewYorkCity"

    def test_drops_punctuation(self):
        # Punctuation collapses into an underscore; the trailing `!` is stripped.
        assert sanitize_token("Highway #4!") == "Highway_4"
        # 'O'Brien' (apostrophe stripped, then NFKD fold) → "OBrien"
        assert sanitize_token("O'Brien") == "OBrien"

    def test_empty_input(self):
        assert sanitize_token("") == ""
        assert sanitize_token(None) == ""  # type: ignore[arg-type]


class TestRoadNaming:
    def test_road_with_ref_prefers_ref(self, namer):
        name = namer.make_name(
            "Road",
            properties={"ref": "E4", "name": "Storgatan", "surface": "asphalt"},
            x_local=1000, z_local=1000,
        )
        assert name == "Road_E4_Asphalt"

    def test_road_with_only_name_uses_name(self, namer):
        name = namer.make_name(
            "Road",
            properties={"name": "Storgatan", "surface": "asphalt"},
            x_local=1000, z_local=1000,
        )
        assert name == "Road_Storgatan_Asphalt"

    def test_anonymous_road_quadrant_indexed(self, namer):
        # NE quadrant on a 4096x4096 terrain → x>=2048, z>=2048
        name1 = namer.make_name(
            "Road", properties={"surface": "asphalt"},
            x_local=3000, z_local=3000,
        )
        name2 = namer.make_name(
            "Road", properties={"surface": "asphalt"},
            x_local=3100, z_local=3100,
        )
        assert name1 == "Road_Asphalt_NE_001"
        assert name2 == "Road_Asphalt_NE_002"

    def test_anonymous_road_different_quadrants(self, namer):
        nw = namer.make_name(
            "Road", properties={"surface": "asphalt"},
            x_local=500, z_local=3000,
        )
        sw = namer.make_name(
            "Road", properties={"surface": "asphalt"},
            x_local=500, z_local=500,
        )
        assert "NW" in nw and "SW" in sw
        # Both start their own _001 counter because the quadrant differs.
        assert nw.endswith("_001") and sw.endswith("_001")

    def test_road_name_with_unicode(self, namer):
        # Ä, Ö, etc. get folded to ASCII.
        name = namer.make_name(
            "Road",
            properties={"name": "Övre Storgatan", "surface": "asphalt"},
            x_local=1000, z_local=1000,
        )
        assert name == "Road_OvreStorgatan_Asphalt"


class TestForestNaming:
    def test_coniferous_named_pine(self, namer):
        name = namer.make_name(
            "Forest",
            properties={"forest_type": "coniferous"},
            x_local=3000, z_local=3000,
        )
        assert name == "Forest_Pine_NE_001"

    def test_deciduous_named_deciduous(self, namer):
        name = namer.make_name(
            "Forest",
            properties={"forest_type": "deciduous"},
            x_local=500, z_local=500,
        )
        assert name == "Forest_Deciduous_SW_001"

    def test_unknown_forest_type_defaults_mixed(self, namer):
        name = namer.make_name(
            "Forest",
            properties={},
            x_local=1000, z_local=1000,
        )
        assert "Mixed" in name


class TestLakeNaming:
    def test_named_lake_uses_name(self, namer):
        name = namer.make_name(
            "Lake",
            properties={"name": "Vänern", "water_type": "lake"},
            x_local=1000, z_local=1000,
        )
        assert name == "Lake_Vanern"

    def test_anonymous_lake_quadrant_indexed(self, namer):
        name = namer.make_name(
            "Lake",
            properties={"water_type": "lake"},
            x_local=500, z_local=3000,
        )
        assert name == "Lake_NW_001"

    def test_two_lakes_with_same_name_get_suffix(self, namer):
        a = namer.make_name(
            "Lake",
            properties={"name": "Vänern"},
            x_local=1000, z_local=1000,
        )
        b = namer.make_name(
            "Lake",
            properties={"name": "Vänern"},
            x_local=2000, z_local=2000,
        )
        assert a == "Lake_Vanern"
        assert b == "Lake_Vanern_002"


class TestRiverNaming:
    def test_named_river_uses_name(self, namer):
        name = namer.make_name(
            "River",
            properties={"name": "Dalälven", "water_type": "river"},
            x_local=1000, z_local=1000,
        )
        assert name == "River_Dalalven"

    def test_anonymous_river_collapses_category(self, namer):
        # Avoid the ugly River_River_<quad>_NNN.
        name = namer.make_name(
            "River",
            properties={"water_type": "river"},
            x_local=3000, z_local=3000,
        )
        assert name == "River_NE_001"


class TestBuildingNaming:
    def test_named_church(self, namer):
        name = namer.make_name(
            "Building",
            properties={"name": "St Mary", "building_type": "church"},
            x_local=1000, z_local=1000,
        )
        assert name == "Building_Church_StMary"

    def test_anonymous_residential(self, namer):
        name = namer.make_name(
            "Building",
            properties={"building_type": "residential"},
            x_local=500, z_local=3000,
        )
        assert name == "Building_Residential_NW_001"


class TestExpectedSurface:
    def test_asphalt_road_paints_asphalt(self):
        assert expected_surface("Road", {"surface": "asphalt"}) == "asphalt"

    def test_gravel_road_paints_gravel(self):
        assert expected_surface("Road", {"surface": "gravel"}) == "gravel"

    def test_dirt_road_paints_dirt(self):
        assert expected_surface("Road", {"surface": "dirt"}) == "dirt"

    def test_cobblestone_road_paints_asphalt(self):
        # Cobblestone surface mask isn't generated; closest match is asphalt.
        assert expected_surface("Road", {"surface": "cobblestone"}) == "asphalt"

    def test_coniferous_forest_paints_pine_floor(self):
        assert expected_surface("Forest", {"forest_type": "coniferous"}) == "pine_floor"

    def test_deciduous_forest_paints_forest_floor(self):
        assert expected_surface("Forest", {"forest_type": "deciduous"}) == "forest_floor"

    def test_lake_paints_water_edge(self):
        assert expected_surface("Lake", {}) == "water_edge"

    def test_river_paints_water_edge(self):
        assert expected_surface("River", {}) == "water_edge"

    def test_building_no_surface(self):
        # Buildings sit on whatever surface the painter put underneath.
        assert expected_surface("Building", {}) is None
