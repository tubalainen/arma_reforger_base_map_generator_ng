"""
Shared test fixtures for the map generator test suite.

Provides synthetic test data (GeoJSON features, bbox dicts, etc.)
so that unit tests can run without network access or heavy dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the webapp directory is on sys.path so service imports work
WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


# ---------------------------------------------------------------------------
# Bounding box fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_bbox_dict():
    """A small bounding box in southern Norway (Kristiansand area)."""
    return {
        "west": 7.90,
        "south": 58.10,
        "east": 8.10,
        "north": 58.25,
    }


@pytest.fixture
def sample_bbox_tuple():
    """Same area as sample_bbox_dict but as (west, south, east, north) tuple."""
    return (7.90, 58.10, 8.10, 58.25)


# ---------------------------------------------------------------------------
# Polygon coordinate fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def norway_polygon_coords():
    """A polygon covering part of southern Norway."""
    return [
        [7.90, 58.10],
        [8.10, 58.10],
        [8.10, 58.25],
        [7.90, 58.25],
        [7.90, 58.10],
    ]


@pytest.fixture
def sweden_polygon_coords():
    """A polygon covering part of central Sweden."""
    return [
        [16.0, 59.0],
        [16.5, 59.0],
        [16.5, 59.3],
        [16.0, 59.3],
        [16.0, 59.0],
    ]


@pytest.fixture
def cross_border_polygon_coords():
    """A polygon that crosses the Norway-Sweden border."""
    return [
        [11.5, 59.0],
        [12.5, 59.0],
        [12.5, 60.0],
        [11.5, 60.0],
        [11.5, 59.0],
    ]


# ---------------------------------------------------------------------------
# GeoJSON fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_road_features():
    """Minimal GeoJSON FeatureCollection with a few roads."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[7.95, 58.15], [8.00, 58.18], [8.05, 58.20]],
                },
                "properties": {
                    "osm_id": 1001,
                    "highway": "primary",
                    "surface": "asphalt",
                    "width": "7",
                    "name": "E39",
                    "bridge": "no",
                    "tunnel": "no",
                    "lanes": "2",
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[7.92, 58.12], [7.96, 58.14]],
                },
                "properties": {
                    "osm_id": 1002,
                    "highway": "track",
                    "surface": "",
                    "width": "",
                    "name": "",
                    "bridge": "no",
                    "tunnel": "no",
                    "lanes": "",
                },
            },
        ],
    }


@pytest.fixture
def sample_water_features():
    """Minimal GeoJSON FeatureCollection with a lake and a river."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [7.95, 58.16], [7.96, 58.16],
                            [7.96, 58.17], [7.95, 58.17],
                            [7.95, 58.16],
                        ]
                    ],
                },
                "properties": {
                    "osm_id": 2001,
                    "water_type": "lake",
                    "name": "Test Lake",
                    "natural": "water",
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[7.98, 58.13], [7.99, 58.15], [8.00, 58.17]],
                },
                "properties": {
                    "osm_id": 2002,
                    "water_type": "river",
                    "name": "Test River",
                    "waterway": "river",
                    "intermittent": "no",
                },
            },
        ],
    }


@pytest.fixture
def sample_forest_features():
    """Minimal GeoJSON FeatureCollection with a forest area."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [7.93, 58.18], [7.97, 58.18],
                            [7.97, 58.22], [7.93, 58.22],
                            [7.93, 58.18],
                        ]
                    ],
                },
                "properties": {
                    "osm_id": 3001,
                    "category": "forest",
                    "type": "wood",
                    "leaf_type": "needleleaved",
                    "name": "Nordmarka Test",
                },
            },
        ],
    }


@pytest.fixture
def sample_building_features():
    """Minimal GeoJSON FeatureCollection with building footprints."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [8.00, 58.15], [8.001, 58.15],
                            [8.001, 58.151], [8.00, 58.151],
                            [8.00, 58.15],
                        ]
                    ],
                },
                "properties": {
                    "osm_id": 4001,
                    "building_type": "house",
                    "height": 6,
                    "name": "Test House",
                },
            },
        ],
    }
