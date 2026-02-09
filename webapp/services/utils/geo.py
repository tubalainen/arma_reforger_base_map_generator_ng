"""
Geographic coordinate and bounding box utilities.

Shared helpers for CRS transformations, bbox conversions,
and distance/dimension estimations.
"""

from __future__ import annotations

import math

from pyproj import Transformer


def transform_bbox_to_crs(
    bbox_wgs84: tuple[float, float, float, float],
    target_crs: str,
) -> tuple[float, float, float, float]:
    """Transform a WGS84 bbox (west, south, east, north) to a target CRS."""
    transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
    x_min, y_min = transformer.transform(bbox_wgs84[0], bbox_wgs84[1])
    x_max, y_max = transformer.transform(bbox_wgs84[2], bbox_wgs84[3])
    return (x_min, y_min, x_max, y_max)


def bbox_dict_to_tuple(bbox: dict) -> tuple[float, float, float, float]:
    """Convert bbox dict {west, south, east, north} to tuple (west, south, east, north)."""
    return (bbox["west"], bbox["south"], bbox["east"], bbox["north"])


def estimate_bbox_dimensions_m(bbox: dict) -> tuple[float, float]:
    """
    Estimate the width and height of a WGS84 bbox in metres.

    Args:
        bbox: Dict with west, south, east, north in EPSG:4326

    Returns:
        (width_m, height_m)
    """
    lat_mid = (bbox["south"] + bbox["north"]) / 2
    m_per_deg_lat = 111320
    m_per_deg_lng = 111320 * math.cos(math.radians(lat_mid))
    width_m = (bbox["east"] - bbox["west"]) * m_per_deg_lng
    height_m = (bbox["north"] - bbox["south"]) * m_per_deg_lat
    return (width_m, height_m)


def bbox_to_overpass_str(bbox: dict) -> str:
    """Convert bbox dict to Overpass bbox string (south,west,north,east)."""
    return f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"
