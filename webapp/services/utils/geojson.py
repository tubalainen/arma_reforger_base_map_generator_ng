"""
GeoJSON element processing helpers.

Common patterns for converting Overpass API elements to GeoJSON features:
coordinate extraction, ring closing, relation handling, and geometry construction.
"""

from __future__ import annotations


def extract_coords_from_geometry(geometry_points: list[dict]) -> list[list[float]]:
    """
    Extract [lng, lat] coordinate pairs from Overpass geometry points.

    Args:
        geometry_points: List of {"lon": float, "lat": float} dicts from Overpass API

    Returns:
        List of [lng, lat] coordinate pairs
    """
    return [[pt["lon"], pt["lat"]] for pt in geometry_points]


def close_ring(coords: list[list[float]]) -> list[list[float]]:
    """
    Ensure a coordinate ring is closed (first point == last point).

    If the ring is already closed, returns it unchanged.
    If not, appends a copy of the first point.
    """
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def extract_outer_rings_from_relation(element: dict) -> list[list[list[float]]]:
    """
    Extract outer rings from an Overpass relation element.

    Args:
        element: Overpass element with type="relation" and "members" list

    Returns:
        List of coordinate rings (each ring is a list of [lng, lat] pairs)
    """
    outer_rings = []
    for member in element.get("members", []):
        if member.get("role") == "outer" and "geometry" in member:
            ring = extract_coords_from_geometry(member["geometry"])
            if len(ring) > 3:
                close_ring(ring)
                outer_rings.append(ring)
    return outer_rings


def make_polygon_or_multi(rings: list[list[list[float]]]) -> dict:
    """
    Construct a GeoJSON Polygon or MultiPolygon geometry from outer rings.

    Args:
        rings: List of coordinate rings

    Returns:
        GeoJSON geometry dict with type "Polygon" or "MultiPolygon"
    """
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": [rings[0]]}
    return {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}
