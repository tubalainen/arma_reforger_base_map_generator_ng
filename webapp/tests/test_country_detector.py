"""Tests for services/country_detector.py — country detection logic."""

from __future__ import annotations

import pytest


class TestGeometriesLoaded:
    """Verify Natural Earth country geometries load correctly."""

    def test_geometries_loaded(self):
        from services.country_detector import _CODES, _GEOMS
        # NE 10m has ~250 admin-0 features; well over 150 have ISO_A2_EH codes.
        assert len(_CODES) >= 150
        assert len(_CODES) == len(_GEOMS)
        for code in ["SE", "NO", "DK", "FI", "EE", "LV", "LT", "RU", "US", "DE", "FR"]:
            assert code in _CODES


class TestGetCountryForPoint:
    """Test single-point country detection."""

    def test_oslo_is_norway(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(10.75, 59.91) == "NO"

    def test_stockholm_is_sweden(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(18.07, 59.33) == "SE"

    def test_tallinn_is_estonia(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(24.75, 59.44) == "EE"

    def test_copenhagen_is_denmark(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(12.57, 55.68) == "DK"

    def test_helsinki_is_finland(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(24.94, 60.17) == "FI"

    def test_kansas_is_usa(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(-98.0, 39.0) == "US"

    def test_alaska_is_usa(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(-150.0, 64.0) == "US"

    def test_hawaii_is_usa(self):
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(-157.86, 21.31) == "US"

    def test_bjorkoby_is_finland(self):
        # Regression for issue #38 — Björköby (Kvarken archipelago, FI)
        # was previously misclassified as Sweden.
        from services.country_detector import _get_country_for_point
        assert _get_country_for_point(21.5667, 63.3500) == "FI"

    def test_ocean_returns_none(self):
        from services.country_detector import _get_country_for_point
        # Open ocean well west of Ireland
        assert _get_country_for_point(-25.0, 50.0) is None


class TestDetectCountriesPolygon:
    """Test polygon-based country detection (no network)."""

    def test_norway_polygon(self, norway_polygon_coords):
        from services.country_detector import _detect_countries_polygon
        result = _detect_countries_polygon(norway_polygon_coords)
        assert "NO" in result

    def test_sweden_polygon(self, sweden_polygon_coords):
        from services.country_detector import _detect_countries_polygon
        result = _detect_countries_polygon(sweden_polygon_coords)
        assert "SE" in result

    def test_cross_border_detects_both(self, cross_border_polygon_coords):
        from services.country_detector import _detect_countries_polygon
        result = _detect_countries_polygon(cross_border_polygon_coords)
        assert "NO" in result
        assert "SE" in result


class TestDetectCountriesIntegration:
    """End-to-end tests for the public detect_countries() entry point."""

    @pytest.mark.asyncio
    async def test_bjorkoby_returns_finland(self):
        # Regression for issue #38 — small selection over Björköby island
        # (Kvarken archipelago, ~63.35 N 21.57 E) must resolve to FI.
        from services.country_detector import detect_countries
        polygon = [
            [21.54, 63.34],
            [21.60, 63.34],
            [21.60, 63.37],
            [21.54, 63.37],
            [21.54, 63.34],
        ]
        result = await detect_countries(polygon)
        assert result["countries"] == ["FI"]
        assert result["primary_country"] == "FI"
        assert result["crs"] == "EPSG:3067"

    @pytest.mark.asyncio
    async def test_kansas_returns_us(self):
        # Regression for issue #48 — selection in CONUS must offline-resolve
        # to US without falling through to Nominatim retries.
        from services.country_detector import detect_countries
        polygon = [
            [-98.5, 38.5],
            [-97.5, 38.5],
            [-97.5, 39.5],
            [-98.5, 39.5],
            [-98.5, 38.5],
        ]
        result = await detect_countries(polygon)
        assert result["countries"] == ["US"]
        assert result["primary_country"] == "US"

    @pytest.mark.asyncio
    async def test_alaska_returns_us(self):
        # Regression for issue #48 — Alaska must also resolve to US.
        from services.country_detector import detect_countries
        polygon = [
            [-150.5, 63.5],
            [-149.5, 63.5],
            [-149.5, 64.5],
            [-150.5, 64.5],
            [-150.5, 63.5],
        ]
        result = await detect_countries(polygon)
        assert result["countries"] == ["US"]
        assert result["primary_country"] == "US"

    @pytest.mark.asyncio
    async def test_open_ocean_returns_unknown(self):
        from services.country_detector import detect_countries
        polygon = [
            [-25.5, 49.5],
            [-24.5, 49.5],
            [-24.5, 50.5],
            [-25.5, 50.5],
            [-25.5, 49.5],
        ]
        result = await detect_countries(polygon)
        assert result["countries"] == []
        assert result["primary_country"] == "UNKNOWN"


class TestGetCrsForArea:
    """Test CRS selection."""

    def test_single_country_norway(self, norway_polygon_coords):
        from services.country_detector import get_crs_for_area
        crs = get_crs_for_area(["NO"], norway_polygon_coords)
        assert crs == "EPSG:25833"

    def test_single_country_sweden(self, sweden_polygon_coords):
        from services.country_detector import get_crs_for_area
        crs = get_crs_for_area(["SE"], sweden_polygon_coords)
        assert crs == "EPSG:3006"

    def test_single_country_unknown_falls_back_to_utm(self):
        # Country code with no entry in COUNTRY_CRS should fall through to UTM.
        from services.country_detector import get_crs_for_area
        crs = get_crs_for_area(["XX"], [[10, 60], [11, 60], [11, 61], [10, 61], [10, 60]])
        assert crs.startswith("EPSG:326") or crs.startswith("EPSG:327")

    def test_multi_country_picks_dominant(self, cross_border_polygon_coords):
        from services.country_detector import get_crs_for_area
        crs = get_crs_for_area(["NO", "SE"], cross_border_polygon_coords)
        assert crs in ("EPSG:25833", "EPSG:3006")


class TestUtmCrsFromCentroid:
    """Test UTM zone calculation."""

    def test_oslo(self):
        from services.country_detector import _utm_crs_from_centroid
        assert _utm_crs_from_centroid(10.75, 59.91) == "EPSG:32632"

    def test_negative_longitude(self):
        from services.country_detector import _utm_crs_from_centroid
        assert _utm_crs_from_centroid(-73.0, 40.7) == "EPSG:32618"

    def test_southern_hemisphere(self):
        from services.country_detector import _utm_crs_from_centroid
        crs = _utm_crs_from_centroid(18.4, -33.9)
        assert crs.startswith("EPSG:327")
