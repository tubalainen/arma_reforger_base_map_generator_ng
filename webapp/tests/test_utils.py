"""Tests for services/utils/ shared utility modules."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# tests for services.utils.geojson
# ---------------------------------------------------------------------------

class TestExtractCoordsFromGeometry:
    """Test extract_coords_from_geometry()."""

    def test_basic_extraction(self):
        from services.utils.geojson import extract_coords_from_geometry
        points = [{"lon": 10.0, "lat": 60.0}, {"lon": 11.0, "lat": 61.0}]
        result = extract_coords_from_geometry(points)
        assert result == [[10.0, 60.0], [11.0, 61.0]]

    def test_empty_input(self):
        from services.utils.geojson import extract_coords_from_geometry
        assert extract_coords_from_geometry([]) == []

    def test_single_point(self):
        from services.utils.geojson import extract_coords_from_geometry
        points = [{"lon": 5.5, "lat": 58.0}]
        assert extract_coords_from_geometry(points) == [[5.5, 58.0]]


class TestCloseRing:
    """Test close_ring()."""

    def test_unclosed_ring(self):
        from services.utils.geojson import close_ring
        ring = [[1, 2], [3, 4], [5, 6]]
        close_ring(ring)
        assert ring[-1] == [1, 2]
        assert len(ring) == 4

    def test_already_closed_ring(self):
        from services.utils.geojson import close_ring
        ring = [[1, 2], [3, 4], [5, 6], [1, 2]]
        close_ring(ring)
        assert len(ring) == 4  # should not double-close

    def test_empty_ring(self):
        from services.utils.geojson import close_ring
        ring = []
        close_ring(ring)
        assert ring == []


class TestMakePolygonOrMulti:
    """Test make_polygon_or_multi()."""

    def test_single_ring_makes_polygon(self):
        from services.utils.geojson import make_polygon_or_multi
        ring = [[1, 2], [3, 4], [5, 6], [1, 2]]
        geom = make_polygon_or_multi([ring])
        assert geom["type"] == "Polygon"
        assert geom["coordinates"] == [ring]

    def test_multiple_rings_makes_multipolygon(self):
        from services.utils.geojson import make_polygon_or_multi
        ring1 = [[1, 2], [3, 4], [1, 2]]
        ring2 = [[5, 6], [7, 8], [5, 6]]
        geom = make_polygon_or_multi([ring1, ring2])
        assert geom["type"] == "MultiPolygon"
        assert len(geom["coordinates"]) == 2


class TestExtractOuterRingsFromRelation:
    """Test extract_outer_rings_from_relation()."""

    def test_basic_relation(self):
        from services.utils.geojson import extract_outer_rings_from_relation
        element = {
            "type": "relation",
            "members": [
                {
                    "role": "outer",
                    "geometry": [
                        {"lon": 10.0, "lat": 60.0},
                        {"lon": 11.0, "lat": 60.0},
                        {"lon": 11.0, "lat": 61.0},
                        {"lon": 10.0, "lat": 61.0},
                        {"lon": 10.0, "lat": 60.0},
                    ],
                },
                {
                    "role": "inner",
                    "geometry": [
                        {"lon": 10.2, "lat": 60.2},
                        {"lon": 10.8, "lat": 60.2},
                        {"lon": 10.8, "lat": 60.8},
                        {"lon": 10.2, "lat": 60.8},
                        {"lon": 10.2, "lat": 60.2},
                    ],
                },
            ],
        }
        rings = extract_outer_rings_from_relation(element)
        assert len(rings) == 1
        assert len(rings[0]) == 5  # 5 vertices including closing

    def test_no_members(self):
        from services.utils.geojson import extract_outer_rings_from_relation
        element = {"type": "relation", "members": []}
        assert extract_outer_rings_from_relation(element) == []

    def test_short_ring_excluded(self):
        from services.utils.geojson import extract_outer_rings_from_relation
        element = {
            "type": "relation",
            "members": [
                {
                    "role": "outer",
                    "geometry": [
                        {"lon": 10.0, "lat": 60.0},
                        {"lon": 11.0, "lat": 60.0},
                    ],
                },
            ],
        }
        # Only 2 points, should be excluded (needs > 3)
        rings = extract_outer_rings_from_relation(element)
        assert len(rings) == 0


# ---------------------------------------------------------------------------
# tests for services.utils.geo
# ---------------------------------------------------------------------------

class TestBboxToOverpassStr:
    """Test bbox_to_overpass_str()."""

    def test_basic_conversion(self, sample_bbox_dict):
        from services.utils.geo import bbox_to_overpass_str
        result = bbox_to_overpass_str(sample_bbox_dict)
        assert result == "58.1,7.9,58.25,8.1"

    def test_negative_coordinates(self):
        from services.utils.geo import bbox_to_overpass_str
        bbox = {"west": -10.5, "south": 51.0, "east": -6.0, "north": 55.4}
        result = bbox_to_overpass_str(bbox)
        assert result == "51.0,-10.5,55.4,-6.0"


class TestBboxDictToTuple:
    """Test bbox_dict_to_tuple()."""

    def test_conversion(self, sample_bbox_dict):
        from services.utils.geo import bbox_dict_to_tuple
        result = bbox_dict_to_tuple(sample_bbox_dict)
        assert result == (7.90, 58.10, 8.10, 58.25)


class TestEstimateBboxDimensionsM:
    """Test estimate_bbox_dimensions_m()."""

    def test_reasonable_dimensions(self, sample_bbox_dict):
        from services.utils.geo import estimate_bbox_dimensions_m
        w, h = estimate_bbox_dimensions_m(sample_bbox_dict)
        # ~0.2 degrees longitude at 58 degrees latitude ≈ 11.8 km
        assert 10_000 < w < 15_000
        # ~0.15 degrees latitude ≈ 16.7 km
        assert 15_000 < h < 20_000
