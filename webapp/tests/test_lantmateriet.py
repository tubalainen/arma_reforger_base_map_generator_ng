"""
Tests for Lantmäteriet API integration.

Tests cover:
- Authentication (basic auth header generation)
- Configuration (LantmaterietConfig, _load_config)
- Elevation service dispatcher (basic auth type + STAC dispatch)
- Satellite service dispatcher (country-aware imagery)
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure webapp is on sys.path
WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestLantmaterietConfig:
    """Test config/lantmateriet.py."""

    def test_config_loads_from_env(self):
        """Test that _load_config reads from environment variables."""
        with patch.dict(os.environ, {
            "LANTMATERIET_USERNAME": "testuser",
            "LANTMATERIET_PASSWORD": "testpass",
        }):
            from config.lantmateriet import _load_config
            config = _load_config()

            assert config.username == "testuser"
            assert config.password == "testpass"
            assert config.has_credentials() is True

    def test_config_empty_credentials(self):
        """Test that has_credentials() returns False when empty."""
        with patch.dict(os.environ, {
            "LANTMATERIET_USERNAME": "",
            "LANTMATERIET_PASSWORD": "",
        }):
            from config.lantmateriet import _load_config
            config = _load_config()

            assert config.has_credentials() is False

    def test_config_partial_credentials(self):
        """Test that has_credentials() returns False with only username."""
        with patch.dict(os.environ, {
            "LANTMATERIET_USERNAME": "testuser",
            "LANTMATERIET_PASSWORD": "",
        }):
            from config.lantmateriet import _load_config
            config = _load_config()

            assert config.has_credentials() is False

    def test_config_default_endpoints(self):
        """Test that default endpoint URLs are set correctly."""
        from config.lantmateriet import _load_config
        config = _load_config()

        assert "stac-hojd" in config.stac_hojd_endpoint
        assert "stac-vektor" in config.stac_vektor_endpoint
        assert "historiska-ortofoton" in config.orthophoto_wms
        assert "topowebb" in config.topowebb_wmts
        assert config.native_crs == "EPSG:3006"

    def test_config_ogc_features_endpoints(self):
        """Test that OGC Features API endpoints are configured."""
        from config.lantmateriet import _load_config
        config = _load_config()

        assert "hydrografi" in config.hydrografi_endpoint
        assert "marktacke" in config.marktacke_endpoint
        assert config.hydrografi_endpoint.startswith("https://api.lantmateriet.se/")
        assert config.marktacke_endpoint.startswith("https://api.lantmateriet.se/")

    def test_config_default_settings(self):
        """Test default elevation resolution and tile size."""
        from config.lantmateriet import _load_config
        config = _load_config()

        assert config.elevation_resolution_m == 1.0
        assert config.max_tile_size == 4096


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestLantmaterietAuth:
    """Test services/lantmateriet/auth.py."""

    def test_basic_auth_header_generation(self):
        """Test basic auth header is correctly Base64-encoded."""
        with patch.dict(os.environ, {
            "LANTMATERIET_USERNAME": "testuser",
            "LANTMATERIET_PASSWORD": "testpass",
        }):
            # Reload config to pick up env vars
            from config.lantmateriet import _load_config
            import config.lantmateriet as lm_config
            lm_config.LANTMATERIET_CONFIG = _load_config()

            from services.lantmateriet.auth import get_basic_auth_header
            header = get_basic_auth_header()

            assert header is not None
            assert "Authorization" in header

            # Verify Base64 encoding
            encoded = header["Authorization"].replace("Basic ", "")
            decoded = base64.b64decode(encoded).decode()
            assert decoded == "testuser:testpass"

    def test_basic_auth_header_missing_credentials(self):
        """Test auth header returns None when credentials missing."""
        with patch.dict(os.environ, {
            "LANTMATERIET_USERNAME": "",
            "LANTMATERIET_PASSWORD": "",
        }):
            from config.lantmateriet import _load_config
            import config.lantmateriet as lm_config
            lm_config.LANTMATERIET_CONFIG = _load_config()

            from services.lantmateriet.auth import get_basic_auth_header
            header = get_basic_auth_header()

            assert header is None

    def test_authenticated_headers_include_user_agent(self):
        """Test that authenticated headers always include User-Agent."""
        from services.lantmateriet.auth import get_authenticated_headers
        headers = get_authenticated_headers()

        assert "User-Agent" in headers
        assert "Accept" in headers
        assert "ArmaReforgerMapGenerator" in headers["User-Agent"]

    def test_authenticated_headers_include_auth_when_credentials_set(self):
        """Test that auth header is included when credentials are available."""
        with patch.dict(os.environ, {
            "LANTMATERIET_USERNAME": "testuser",
            "LANTMATERIET_PASSWORD": "testpass",
        }):
            from config.lantmateriet import _load_config
            import config.lantmateriet as lm_config
            lm_config.LANTMATERIET_CONFIG = _load_config()

            from services.lantmateriet.auth import get_authenticated_headers
            headers = get_authenticated_headers()

            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Basic ")


# ---------------------------------------------------------------------------
# Elevation config tests
# ---------------------------------------------------------------------------


class TestElevationConfig:
    """Test elevation config changes for Sweden."""

    def test_sweden_config_uses_basic_auth(self):
        """Test that SE config has auth_type='basic' (not oauth2)."""
        from config.elevation import ELEVATION_CONFIGS

        se_config = ELEVATION_CONFIGS["SE"]
        assert se_config.auth_type == "basic"
        assert se_config.auth_env_var == "LANTMATERIET_USERNAME"
        assert "password_env_var" in se_config.extra_params
        assert se_config.extra_params["password_env_var"] == "LANTMATERIET_PASSWORD"

    def test_sweden_config_uses_stac_api_type(self):
        """Test that SE config has api_type='stac'."""
        from config.elevation import ELEVATION_CONFIGS

        se_config = ELEVATION_CONFIGS["SE"]
        assert se_config.api_type == "stac"
        assert "stac-hojd" in se_config.endpoint

    def test_sweden_config_native_crs(self):
        """Test that SE config uses SWEREF99 TM."""
        from config.elevation import ELEVATION_CONFIGS

        se_config = ELEVATION_CONFIGS["SE"]
        assert se_config.native_crs == "EPSG:3006"
        assert se_config.resolution_m == 1.0

    def test_lantmateriet_api_key_removed(self):
        """Test that LANTMATERIET_API_KEY is no longer exported."""
        from config import elevation
        assert not hasattr(elevation, "LANTMATERIET_API_KEY")

    def test_other_countries_unchanged(self):
        """Test that non-SE configs remain unchanged."""
        from config.elevation import ELEVATION_CONFIGS

        # Norway should still use WCS 1.0 with no auth
        no_config = ELEVATION_CONFIGS["NO"]
        assert no_config.auth_type == "none"
        assert no_config.api_type == "wcs"
        assert no_config.version == "1.0.0"

        # Finland should still use WCS 2.0.1 with api_key
        fi_config = ELEVATION_CONFIGS["FI"]
        assert fi_config.auth_type == "api_key"
        assert fi_config.api_type == "wcs"


# ---------------------------------------------------------------------------
# Config __init__ re-export tests
# ---------------------------------------------------------------------------


class TestConfigReExports:
    """Test config/__init__.py re-exports."""

    def test_lantmateriet_config_exported(self):
        """Test that LANTMATERIET_CONFIG is exported from config package."""
        from config import LANTMATERIET_CONFIG, LantmaterietConfig

        assert isinstance(LANTMATERIET_CONFIG, LantmaterietConfig)

    def test_lantmateriet_api_key_not_exported(self):
        """Test that old LANTMATERIET_API_KEY is not in config exports."""
        import config
        assert not hasattr(config, "LANTMATERIET_API_KEY")


# ---------------------------------------------------------------------------
# Integration tests (requires real credentials, skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("LANTMATERIET_USERNAME"),
    reason="LANTMATERIET_USERNAME not set",
)
class TestLantmaterietIntegration:
    """Integration tests that require real Lantmäteriet credentials."""

    @pytest.mark.asyncio
    async def test_stac_elevation_fetch(self):
        """Test STAC Höjd elevation fetch for a small Swedish area."""
        from services.lantmateriet.stac_elevation import fetch_stac_elevation

        # Small area in central Stockholm (EPSG:3006 coordinates)
        bbox = (674000, 6580000, 675000, 6581000)  # ~1 km²
        result = await fetch_stac_elevation(bbox, "EPSG:3006")

        assert result is not None
        assert len(result) > 1000  # Should be a substantial GeoTIFF

    @pytest.mark.asyncio
    async def test_orthophoto_fetch(self):
        """Test historical orthophoto fetch for a small Swedish area."""
        from services.lantmateriet.orthophoto_service import (
            fetch_historical_orthophoto,
        )

        bbox = (18.06, 59.33, 18.08, 59.35)  # Stockholm, WGS84
        result = await fetch_historical_orthophoto(bbox, 512, 512)

        assert result is not None
        assert len(result) > 1000  # Should be a PNG image

    # Note: STAC Vektor integration tests removed — replaced by OGC Features
    # API integration (Hydrografi + Marktäcke) below.

    @pytest.mark.asyncio
    async def test_hydrografi_fetch(self):
        """Test Hydrografi API fetch for a small Swedish area."""
        from services.lantmateriet.hydrografi_service import (
            fetch_lantmateriet_water,
        )

        # Small area near Gothenburg with known lakes/streams
        bbox = (11.9, 57.65, 12.0, 57.72)
        result = await fetch_lantmateriet_water(bbox)

        assert result is not None
        assert result["type"] == "FeatureCollection"
        assert len(result["features"]) > 0

        # Verify translated property schema
        for f in result["features"][:5]:
            props = f["properties"]
            assert "water_type" in props
            assert "natural" in props
            assert "osm_id" in props

    @pytest.mark.asyncio
    async def test_marktacke_fetch(self):
        """Test Marktäcke API fetch for a small Swedish area."""
        from services.lantmateriet.marktacke_service import (
            fetch_lantmateriet_land_cover,
        )

        # Small area near Gothenburg
        bbox = (11.9, 57.65, 12.0, 57.72)
        result = await fetch_lantmateriet_land_cover(bbox)

        assert result is not None
        assert "forests" in result
        assert "land_use" in result
        assert "water" in result

        # Should have some forest coverage in this area
        forests = result["forests"]
        assert forests["type"] == "FeatureCollection"


# ---------------------------------------------------------------------------
# Hydrografi service unit tests
# ---------------------------------------------------------------------------


class TestHydrografiTranslation:
    """Test hydrografi_service.py property translation (no network)."""

    def test_standing_water_to_lake(self):
        """StandingWater with no localType defaults to lake."""
        from services.lantmateriet.hydrografi_service import (
            _translate_standing_water,
        )

        features = [
            {
                "id": 1,
                "geometry": {"type": "Polygon", "coordinates": [[[11, 57], [12, 57], [12, 58], [11, 58], [11, 57]]]},
                "properties": {"localType": "", "persistence": "Perennial"},
            }
        ]
        result = _translate_standing_water(features)

        assert len(result) == 1
        assert result[0]["properties"]["water_type"] == "lake"
        assert result[0]["properties"]["natural"] == "water"
        assert result[0]["properties"]["intermittent"] == "no"

    def test_standing_water_pond(self):
        """StandingWater with localType=Damm maps to pond."""
        from services.lantmateriet.hydrografi_service import (
            _translate_standing_water,
        )

        features = [
            {
                "id": 2,
                "geometry": {"type": "Polygon", "coordinates": [[[11, 57], [12, 57], [12, 58], [11, 57]]]},
                "properties": {"localType": "Damm"},
            }
        ]
        result = _translate_standing_water(features)
        assert result[0]["properties"]["water_type"] == "pond"

    def test_watercourse_line_to_river(self):
        """WatercourseLine with localType=Älv maps to river."""
        from services.lantmateriet.hydrografi_service import (
            _translate_watercourse_line,
        )

        features = [
            {
                "id": 10,
                "geometry": {"type": "LineString", "coordinates": [[11, 57], [12, 58]]},
                "properties": {"localType": "Älv", "persistence": "Perennial"},
            }
        ]
        result = _translate_watercourse_line(features)

        assert len(result) == 1
        assert result[0]["properties"]["water_type"] == "river"
        assert result[0]["properties"]["waterway"] == "river"
        assert result[0]["properties"]["natural"] == ""

    def test_watercourse_line_to_stream_default(self):
        """WatercourseLine with unknown localType defaults to stream."""
        from services.lantmateriet.hydrografi_service import (
            _translate_watercourse_line,
        )

        features = [
            {
                "id": 11,
                "geometry": {"type": "LineString", "coordinates": [[11, 57], [12, 58]]},
                "properties": {"localType": "OkändTyp"},
            }
        ]
        result = _translate_watercourse_line(features)
        assert result[0]["properties"]["water_type"] == "stream"

    def test_watercourse_line_canal(self):
        """WatercourseLine with localType=Kanal maps to canal."""
        from services.lantmateriet.hydrografi_service import (
            _translate_watercourse_line,
        )

        features = [
            {
                "id": 12,
                "geometry": {"type": "LineString", "coordinates": [[11, 57], [12, 58]]},
                "properties": {"localType": "Kanal"},
            }
        ]
        result = _translate_watercourse_line(features)
        assert result[0]["properties"]["water_type"] == "canal"
        assert result[0]["properties"]["waterway"] == "canal"

    def test_wetland_translation(self):
        """Wetland features map to water_type=wetland."""
        from services.lantmateriet.hydrografi_service import (
            _translate_wetland,
        )

        features = [
            {
                "id": 20,
                "geometry": {"type": "Polygon", "coordinates": [[[11, 57], [12, 57], [12, 58], [11, 57]]]},
                "properties": {},
            }
        ]
        result = _translate_wetland(features)

        assert len(result) == 1
        assert result[0]["properties"]["water_type"] == "wetland"
        assert result[0]["properties"]["natural"] == "wetland"

    def test_watercourse_polygon_to_river(self):
        """WatercoursePolygon always maps to water_type=river."""
        from services.lantmateriet.hydrografi_service import (
            _translate_watercourse_polygon,
        )

        features = [
            {
                "id": 30,
                "geometry": {"type": "Polygon", "coordinates": [[[11, 57], [12, 57], [12, 58], [11, 57]]]},
                "properties": {"persistence": "Perennial"},
            }
        ]
        result = _translate_watercourse_polygon(features)

        assert result[0]["properties"]["water_type"] == "river"
        assert result[0]["properties"]["natural"] == "water"

    def test_name_extraction_from_geographical_name(self):
        """Name is extracted from geographicalName array."""
        from services.lantmateriet.hydrografi_service import _extract_name

        # Object-style geographicalName
        feature = {
            "properties": {
                "geographicalName": [
                    {"text": "Vänern", "language": "swe"}
                ]
            }
        }
        assert _extract_name(feature) == "Vänern"

    def test_name_extraction_from_string_array(self):
        """Name is extracted from simple string array."""
        from services.lantmateriet.hydrografi_service import _extract_name

        feature = {
            "properties": {
                "geographicalName": ["Mälaren"]
            }
        }
        assert _extract_name(feature) == "Mälaren"

    def test_name_extraction_from_text_array(self):
        """Name is extracted from geographicalName.text array."""
        from services.lantmateriet.hydrografi_service import _extract_name

        feature = {
            "properties": {
                "geographicalName.text": ["Hjälmaren"]
            }
        }
        assert _extract_name(feature) == "Hjälmaren"

    def test_name_extraction_missing(self):
        """Empty string returned when no name available."""
        from services.lantmateriet.hydrografi_service import _extract_name

        feature = {"properties": {}}
        assert _extract_name(feature) == ""

    def test_persistence_intermittent(self):
        """Intermittent persistence maps to intermittent=yes."""
        from services.lantmateriet.hydrografi_service import (
            _extract_persistence,
        )

        feature = {"properties": {"persistence": "Intermittent"}}
        assert _extract_persistence(feature) == "yes"

    def test_persistence_perennial(self):
        """Perennial persistence maps to intermittent=no."""
        from services.lantmateriet.hydrografi_service import (
            _extract_persistence,
        )

        feature = {"properties": {"persistence": "Perennial"}}
        assert _extract_persistence(feature) == "no"


# ---------------------------------------------------------------------------
# Marktäcke service unit tests
# ---------------------------------------------------------------------------


class TestMarktackeTranslation:
    """Test marktacke_service.py property translation (no network)."""

    def _make_feature(self, objekttypnr, objekttyp=""):
        """Helper to create a minimal Markytor feature."""
        return {
            "id": objekttypnr * 100,
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[11, 57], [12, 57], [12, 58], [11, 58], [11, 57]]],
            },
            "properties": {
                "objekttypnr": objekttypnr,
                "objekttyp": objekttyp,
                "objektidentitet": f"id-{objekttypnr}",
            },
        }

    def test_barr_blandskog_to_needleleaved_forest(self):
        """objekttypnr 2645 → forests with leaf_type=needleleaved."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2645, "Barr- och blandskog")]
        result = _translate_markytor(features)

        assert len(result["forests"]) == 1
        assert result["forests"][0]["properties"]["leaf_type"] == "needleleaved"
        assert result["forests"][0]["properties"]["type"] == "forest"

    def test_lovskog_to_broadleaved_forest(self):
        """objekttypnr 2646 → forests with leaf_type=broadleaved."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2646, "Lövskog")]
        result = _translate_markytor(features)

        assert len(result["forests"]) == 1
        assert result["forests"][0]["properties"]["leaf_type"] == "broadleaved"

    def test_fjallbjorkskog_to_broadleaved_forest(self):
        """objekttypnr 2647 → forests with leaf_type=broadleaved."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2647, "Fjällbjörkskog")]
        result = _translate_markytor(features)

        assert len(result["forests"]) == 1
        assert result["forests"][0]["properties"]["leaf_type"] == "broadleaved"

    def test_aker_to_farmland(self):
        """objekttypnr 2642 → land_use with type=farmland."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2642, "Åker")]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "farmland"
        assert result["land_use"][0]["properties"]["category"] == "landuse"

    def test_fruktodling_to_orchard(self):
        """objekttypnr 2643 → land_use with type=orchard."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2643, "Fruktodling")]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "orchard"

    def test_bebyggelse_to_residential(self):
        """objekttypnr 2636-2638 → land_use with type=residential."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        for nr in (2636, 2637, 2638):
            features = [self._make_feature(nr)]
            result = _translate_markytor(features)
            assert len(result["land_use"]) == 1
            assert result["land_use"][0]["properties"]["type"] == "residential"

    def test_industri_to_industrial(self):
        """objekttypnr 2639 → land_use with type=industrial."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2639)]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "industrial"

    def test_torg_to_retail(self):
        """objekttypnr 2641 → land_use with type=retail."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2641)]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "retail"

    def test_oppen_mark_to_grassland(self):
        """objekttypnr 2640 → land_use with type=grassland."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2640)]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "grassland"

    def test_kalfjall_to_bare_rock(self):
        """objekttypnr 2644 → land_use with type=bare_rock."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2644)]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "bare_rock"

    def test_glaciar_to_bare_rock(self):
        """objekttypnr 2635 → land_use with type=bare_rock."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2635)]
        result = _translate_markytor(features)

        assert len(result["land_use"]) == 1
        assert result["land_use"][0]["properties"]["type"] == "bare_rock"

    def test_sjo_to_water(self):
        """objekttypnr 2632 → water with water_type=lake."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2632)]
        result = _translate_markytor(features)

        assert len(result["water"]) == 1
        assert result["water"][0]["properties"]["water_type"] == "lake"
        assert result["water"][0]["properties"]["natural"] == "water"

    def test_hav_to_coastline(self):
        """objekttypnr 2631 → water with water_type=coastline."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2631)]
        result = _translate_markytor(features)

        assert len(result["water"]) == 1
        assert result["water"][0]["properties"]["water_type"] == "coastline"

    def test_vattendragsyta_to_river(self):
        """objekttypnr 2633 → water with water_type=river."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2633)]
        result = _translate_markytor(features)

        assert len(result["water"]) == 1
        assert result["water"][0]["properties"]["water_type"] == "river"

    def test_anlagt_vatten_to_reservoir(self):
        """objekttypnr 2634 → water with water_type=reservoir."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2634)]
        result = _translate_markytor(features)

        assert len(result["water"]) == 1
        assert result["water"][0]["properties"]["water_type"] == "reservoir"

    def test_ej_karterat_skipped(self):
        """objekttypnr 2648 (unmapped) is skipped entirely."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2648)]
        result = _translate_markytor(features)

        assert len(result["forests"]) == 0
        assert len(result["land_use"]) == 0
        assert len(result["water"]) == 0

    def test_unknown_objekttypnr_skipped(self):
        """Unknown objekttypnr values are skipped."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(9999)]
        result = _translate_markytor(features)

        assert len(result["forests"]) == 0
        assert len(result["land_use"]) == 0
        assert len(result["water"]) == 0

    def test_mixed_features_sorted_to_buckets(self):
        """Mixed features get sorted into correct buckets."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [
            self._make_feature(2645, "Barr- och blandskog"),  # → forests
            self._make_feature(2642, "Åker"),                  # → land_use
            self._make_feature(2632, "Sjö"),                   # → water
            self._make_feature(2636, "Sluten bebyggelse"),     # → land_use
            self._make_feature(2648, "Ej karterat"),           # → skip
        ]
        result = _translate_markytor(features)

        assert len(result["forests"]) == 1
        assert len(result["land_use"]) == 2
        assert len(result["water"]) == 1

    def test_all_objekttyp_mappings_covered(self):
        """All 18 objekttypnr codes (2631-2648) are in the mapping table."""
        from services.lantmateriet.marktacke_service import _OBJEKTTYP_MAP

        for nr in range(2631, 2649):
            assert nr in _OBJEKTTYP_MAP, f"objekttypnr {nr} missing from _OBJEKTTYP_MAP"

    def test_sankmarksytor_to_wetland(self):
        """Sankmarksytor features map to water with water_type=wetland."""
        from services.lantmateriet.marktacke_service import (
            _translate_sankmarksytor,
        )

        features = [
            {
                "id": 100,
                "geometry": {"type": "Polygon", "coordinates": [[[11, 57], [12, 57], [12, 58], [11, 57]]]},
                "properties": {"objekttypnr": 1, "objektidentitet": "wetland-1"},
            }
        ]
        result = _translate_sankmarksytor(features)

        assert len(result) == 1
        assert result[0]["properties"]["water_type"] == "wetland"
        assert result[0]["properties"]["natural"] == "wetland"

    def test_forest_features_have_wood_type(self):
        """Forest features include wood_type property for downstream compat."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2645)]
        result = _translate_markytor(features)

        assert "wood_type" in result["forests"][0]["properties"]

    def test_water_features_have_required_properties(self):
        """Water features from Marktäcke include all required properties."""
        from services.lantmateriet.marktacke_service import _translate_markytor

        features = [self._make_feature(2632)]
        result = _translate_markytor(features)

        props = result["water"][0]["properties"]
        assert "water_type" in props
        assert "natural" in props
        assert "waterway" in props
        assert "intermittent" in props
        assert "osm_id" in props
        assert "name" in props


# ---------------------------------------------------------------------------
# Feature dispatch unit tests
# ---------------------------------------------------------------------------


class TestFeatureDispatch:
    """Test step_fetch_features dispatch logic in map_generator (no network)."""

    def test_empty_feature_collection_helper(self):
        """Test _empty_fc() returns valid empty FeatureCollection."""
        from services.map_generator import _empty_fc

        fc = _empty_fc()
        assert fc["type"] == "FeatureCollection"
        assert fc["features"] == []

    def test_merge_feature_collections(self):
        """Test _merge_feature_collections merges features from two FCs."""
        from services.map_generator import _merge_feature_collections

        fc1 = {"type": "FeatureCollection", "features": [{"id": 1}]}
        fc2 = {"type": "FeatureCollection", "features": [{"id": 2}, {"id": 3}]}
        merged = _merge_feature_collections(fc1, fc2)

        assert len(merged["features"]) == 3
        assert merged["type"] == "FeatureCollection"

    def test_safe_result_with_exception(self):
        """Test _safe_result handles exceptions gracefully."""
        from services.map_generator import _safe_result

        result = _safe_result(ValueError("test error"), "test_feature")
        assert result["type"] == "FeatureCollection"
        assert result["features"] == []

    def test_safe_result_with_none(self):
        """Test _safe_result handles None gracefully."""
        from services.map_generator import _safe_result

        result = _safe_result(None, "test_feature")
        assert result["type"] == "FeatureCollection"
        assert result["features"] == []

    def test_safe_result_with_valid_data(self):
        """Test _safe_result passes through valid data."""
        from services.map_generator import _safe_result

        fc = {"type": "FeatureCollection", "features": [{"id": 1}]}
        result = _safe_result(fc, "test_feature")
        assert result == fc
