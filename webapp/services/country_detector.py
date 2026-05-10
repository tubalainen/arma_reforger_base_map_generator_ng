"""
Detect which country/countries a user-drawn polygon falls in.

Uses Natural Earth 1:10m Admin 0 — Countries (public domain) loaded once
at import time into a Shapely STRtree for fast offline spatial lookup.
No network calls; the dataset covers every country worldwide.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from shapely.geometry import Polygon, Point, shape
from shapely.strtree import STRtree

from config import COUNTRY_CRS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Load Natural Earth 10m country geometries at import time.
# ---------------------------------------------------------------------------

_GEOJSON_FILE = (
    Path(__file__).parent.parent / "data" / "ne_10m_admin_0_countries.geojson"
)


def _load_country_geometries() -> tuple[list[str], list, STRtree]:
    """Load country geometries and build an STRtree for fast lookup.

    Returns (codes, geoms, tree) where codes[i] is the ISO A2 of geoms[i],
    and the STRtree's integer query results index into both lists.
    """
    try:
        data = json.loads(_GEOJSON_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Failed to load {_GEOJSON_FILE}: {exc}")
        return [], [], STRtree([])

    codes: list[str] = []
    geoms: list = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        # ISO_A2_EH is the "Egypt/Hala'ib"-corrected ISO A2; preferred over
        # ISO_A2, which has -99 placeholders for several disputed entries.
        code = props.get("ISO_A2_EH") or props.get("ISO_A2")
        if not code or code == "-99":
            continue
        try:
            geom = shape(feature["geometry"])
        except Exception:
            continue
        codes.append(code.upper())
        geoms.append(geom)

    return codes, geoms, STRtree(geoms)


_CODES, _GEOMS, _TREE = _load_country_geometries()
_CODE_TO_GEOM: dict[str, list] = {}
for _i, _c in enumerate(_CODES):
    _CODE_TO_GEOM.setdefault(_c, []).append(_GEOMS[_i])


# Tolerance (in degrees) for the nearest-country fallback. NE 10m simplifies
# coastlines and drops sub-kilometre islands, so points right on a coast or on
# small archipelago islands (e.g. Björköby in the Kvarken — issue #38) can
# fall just outside any polygon. ~0.25° ≈ 25 km at mid-latitudes is small
# enough not to misattribute open-ocean points yet large enough to recover
# every legitimate island miss observed in practice.
_NEAREST_COUNTRY_TOLERANCE_DEG = 0.25


def _candidate_indices(geom) -> list[int]:
    """Return STRtree candidate indices for a query geometry."""
    if not _GEOMS:
        return []
    return [int(i) for i in _TREE.query(geom)]


def _nearest_country(geom) -> Optional[str]:
    """Return the ISO A2 of the closest country within the tolerance."""
    if not _GEOMS:
        return None
    try:
        idx = int(_TREE.nearest(geom))
    except Exception:
        return None
    nearest_geom = _GEOMS[idx]
    if nearest_geom.distance(geom) <= _NEAREST_COUNTRY_TOLERANCE_DEG:
        return _CODES[idx]
    return None


def _get_country_for_point(lng: float, lat: float) -> Optional[str]:
    """Determine which country a single point falls in.

    Uses a `contains` check against the candidates returned by the STRtree,
    falling back to the nearest country within ``_NEAREST_COUNTRY_TOLERANCE_DEG``
    so coastal points and small islands missed by NE 10m simplification still
    resolve correctly.
    """
    pt = Point(lng, lat)
    for i in _candidate_indices(pt):
        if _GEOMS[i].contains(pt):
            return _CODES[i]
    return _nearest_country(pt)


def _detect_countries_polygon(polygon_coords: list[list[float]]) -> list[str]:
    """Offline polygon-based country detection. Returns sorted ISO A2 codes."""
    user_poly = Polygon([(c[0], c[1]) for c in polygon_coords])
    found: set[str] = set()
    for i in _candidate_indices(user_poly):
        if _GEOMS[i].intersects(user_poly):
            found.add(_CODES[i])
    if found:
        return sorted(found)
    # Fallback: small selections over coastlines or archipelago islands may
    # not intersect any simplified polygon. Use the nearest country within
    # tolerance instead of returning nothing.
    nearest = _nearest_country(user_poly)
    return [nearest] if nearest else []


def _utm_crs_from_centroid(lng: float, lat: float) -> str:
    """Compute UTM zone EPSG code from centroid coordinates."""
    zone = int((lng + 180) / 6) + 1
    if lat >= 0:
        return f"EPSG:326{zone:02d}"
    else:
        return f"EPSG:327{zone:02d}"


def _intersection_area(user_poly: Polygon, code: str) -> float:
    """Sum of intersection area between user polygon and all parts of country."""
    total = 0.0
    for geom in _CODE_TO_GEOM.get(code, []):
        try:
            total += user_poly.intersection(geom).area
        except Exception:
            continue
    return total


def get_crs_for_area(
    countries: list[str],
    polygon_coords: list[list[float]],
) -> str:
    """Determine the best CRS to use for the given area.

    Single-country: returns the country's preferred CRS (or UTM fallback).
    Multi-country: picks the country with the most spatial overlap.
    """
    if len(countries) == 1:
        code = countries[0]
        if code in COUNTRY_CRS:
            return COUNTRY_CRS[code]
        # Fall through to UTM based on polygon centroid
    elif countries:
        user_poly = Polygon([(c[0], c[1]) for c in polygon_coords])
        best_code = None
        best_area = 0.0
        for code in countries:
            area = _intersection_area(user_poly, code)
            if area > best_area:
                best_area = area
                best_code = code
        if best_code and best_code in COUNTRY_CRS:
            return COUNTRY_CRS[best_code]

    centroid_lng = sum(c[0] for c in polygon_coords) / len(polygon_coords)
    centroid_lat = sum(c[1] for c in polygon_coords) / len(polygon_coords)
    return _utm_crs_from_centroid(centroid_lng, centroid_lat)


async def detect_countries(polygon_coords: list[list[float]]) -> dict:
    """Main entry point: detect countries for a polygon.

    Args:
        polygon_coords: List of [lng, lat] coordinate pairs forming the polygon.

    Returns:
        Dict with:
            - countries: list of ISO 2-letter country codes
            - primary_country: country with the largest intersection area
            - crs: recommended CRS for processing
            - bbox: bounding box dict (west, south, east, north)
    """
    polygon = Polygon([(c[0], c[1]) for c in polygon_coords])
    bounds = polygon.bounds  # (minx/west, miny/south, maxx/east, maxy/north)

    countries = _detect_countries_polygon(polygon_coords)
    logger.info(f"Country detection: {countries}")

    if not countries:
        primary = "UNKNOWN"
    elif len(countries) == 1:
        primary = countries[0]
    else:
        # Pick the country with the largest overlap area; more robust than
        # centroid for selections that straddle a coastline.
        best_code = countries[0]
        best_area = -1.0
        for code in countries:
            area = _intersection_area(polygon, code)
            if area > best_area:
                best_area = area
                best_code = code
        primary = best_code

    crs = get_crs_for_area(countries, polygon_coords)

    return {
        "countries": countries,
        "primary_country": primary,
        "crs": crs,
        "bbox": {
            "west": bounds[0],
            "south": bounds[1],
            "east": bounds[2],
            "north": bounds[3],
        },
    }


def get_data_sources_for_country(country_code: str) -> dict:
    """Return the available data sources for a given country."""
    from config import ELEVATION_CONFIGS

    sources = {
        "country_code": country_code,
        "elevation": {
            "primary": None,
            "fallback": "opentopography_cop30",
        },
        "roads": "overpass_api",
        "water": "overpass_api",
        "forests": "overpass_api",
        "buildings": "overpass_api",
        "land_use": "overpass_api",
        "satellite": "sentinel2_cloudless",
    }

    if country_code in ELEVATION_CONFIGS:
        cfg = ELEVATION_CONFIGS[country_code]
        if cfg.auth_type == "none" or os.environ.get(cfg.auth_env_var, ""):
            sources["elevation"]["primary"] = f"{country_code.lower()}_national_dem"

    return sources
