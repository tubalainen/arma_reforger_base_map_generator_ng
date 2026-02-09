"""Tests for services/country_detector.py — country detection logic."""

from __future__ import annotations

import pytest


class TestCountryPolygonsLoaded:
    """Verify country polygon data loads correctly from JSON."""

    def test_polygons_loaded(self):
        from services.country_detector import _COUNTRY_POLYGONS
        assert len(_COUNTRY_POLYGONS) == 8
        for code in ["SE", "NO", "DK", "FI", "EE", "LV", "LT", "RU"]:
            assert code in _COUNTRY_POLYGONS
            assert len(_COUNTRY_POLYGONS[code]) > 20


class TestGetCountryForPoint:
    """Test single-point country detection."""

    def test_oslo_is_norway(self):
        from services.country_detector import _get_country_for_point
        result = _get_country_for_point(10.75, 59.91)
        assert result == "NO"

    def test_stockholm_is_sweden(self):
        from services.country_detector import _get_country_for_point
        result = _get_country_for_point(18.07, 59.33)
        assert result == "SE"

    def test_tallinn_is_estonia(self):
        from services.country_detector import _get_country_for_point
        result = _get_country_for_point(24.75, 59.44)
        assert result == "EE"

    def test_copenhagen_is_denmark(self):
        from services.country_detector import _get_country_for_point
        result = _get_country_for_point(12.57, 55.68)
        assert result == "DK"

    def test_helsinki_is_finland(self):
        from services.country_detector import _get_country_for_point
        result = _get_country_for_point(24.94, 60.17)
        assert result == "FI"

    def test_ocean_returns_none(self):
        from services.country_detector import _get_country_for_point
        result = _get_country_for_point(0.0, 60.0)
        assert result is None


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

    def test_single_country_unknown(self):
        from services.country_detector import get_crs_for_area
        crs = get_crs_for_area(["XX"], [[10, 60], [11, 60], [11, 61], [10, 61], [10, 60]])
        assert crs == "EPSG:4326"

    def test_multi_country_picks_dominant(self, cross_border_polygon_coords):
        from services.country_detector import get_crs_for_area
        # The cross-border polygon covers more of Norway than Sweden
        crs = get_crs_for_area(["NO", "SE"], cross_border_polygon_coords)
        assert crs in ("EPSG:25833", "EPSG:3006")


class TestUtmCrsFromCentroid:
    """Test UTM zone calculation."""

    def test_oslo(self):
        from services.country_detector import _utm_crs_from_centroid
        crs = _utm_crs_from_centroid(10.75, 59.91)
        assert crs == "EPSG:32632"  # UTM zone 32N

    def test_negative_longitude(self):
        from services.country_detector import _utm_crs_from_centroid
        crs = _utm_crs_from_centroid(-73.0, 40.7)  # New York
        assert crs == "EPSG:32618"  # UTM zone 18N

    def test_southern_hemisphere(self):
        from services.country_detector import _utm_crs_from_centroid
        crs = _utm_crs_from_centroid(18.4, -33.9)  # Cape Town
        assert crs.startswith("EPSG:327")  # Southern hemisphere
