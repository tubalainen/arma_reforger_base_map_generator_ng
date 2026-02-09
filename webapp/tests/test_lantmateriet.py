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

    # Note: STAC Vektor integration tests removed — the STAC Vektor API
    # provides municipality-level bulk downloads (ZIP/GeoPackage), not
    # feature-level queries. OSM Overpass is used for all map features.
