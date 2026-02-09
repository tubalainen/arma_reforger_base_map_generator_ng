"""
Detect which country/countries a user-drawn polygon falls in.

Primary method: offline shapely polygon intersection (fast, no network).
Fallback: Nominatim reverse geocoding for countries not in the polygon set.

Country boundary polygon data is loaded from data/country_polygons.json
rather than being embedded inline, keeping this module focused on logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from shapely.geometry import Polygon, Point, box

from config import COUNTRY_BOUNDS, COUNTRY_CRS

logger = logging.getLogger(__name__)

# Retry configuration for Nominatim
NOMINATIM_MAX_RETRIES = 3
NOMINATIM_RETRY_WAIT_S = 3.0
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)


# ---------------------------------------------------------------------------
# Load detailed country boundary polygons from external JSON data file.
# These cover the Nordic + Baltic countries with high-resolution vertices
# for accurate detection without needing network calls.
# ---------------------------------------------------------------------------

_POLYGON_FILE = Path(__file__).parent.parent / "data" / "country_polygons.json"


def _load_country_polygons() -> dict[str, list[tuple[float, float]]]:
    """Load country polygon data from JSON file."""
    try:
        data = json.loads(_POLYGON_FILE.read_text())
        # Convert [lng, lat] lists back to (lng, lat) tuples for shapely
        return {
            code: [(coord[0], coord[1]) for coord in coords]
            for code, coords in data.items()
        }
    except Exception as e:
        logger.error(f"Failed to load country polygons from {_POLYGON_FILE}: {e}")
        return {}


_COUNTRY_POLYGONS: dict[str, list[tuple[float, float]]] = _load_country_polygons()


def _get_country_polygon(country_code: str) -> Optional[Polygon]:
    """Get the simplified boundary polygon for a country, if available."""
    coords = _COUNTRY_POLYGONS.get(country_code)
    if coords is None:
        return None
    return Polygon(coords)


def _get_country_for_point(lng: float, lat: float) -> Optional[str]:
    """Determine which country a single point falls in using polygon data."""
    pt = Point(lng, lat)
    for code in COUNTRY_BOUNDS:
        s, w, n, e = COUNTRY_BOUNDS[code]
        if not (w <= lng <= e and s <= lat <= n):
            continue
        country_poly = _get_country_polygon(code)
        if country_poly is not None and country_poly.contains(pt):
            return code
    return None


def _detect_countries_polygon(polygon_coords: list[list[float]]) -> list[str]:
    """
    Offline polygon-based country detection (fast, no network).

    Uses detailed country outlines for Nordic/Baltic countries and Russia.
    Only falls back to bounding-box intersection for countries without
    detailed polygons.

    Priority order:
    1. Detailed polygon intersection (most accurate)
    2. Bounding-box intersection (fallback for countries without detailed polygons)
    """
    user_poly = Polygon([(c[0], c[1]) for c in polygon_coords])
    detected_detailed = []  # Countries detected by detailed polygon
    detected_bbox = []      # Countries detected by bounding box only

    for code, (s, w, n, e) in COUNTRY_BOUNDS.items():
        country_bbox = box(w, s, e, n)
        if not user_poly.intersects(country_bbox):
            continue

        # Detailed polygon check if available (preferred)
        country_poly = _get_country_polygon(code)
        if country_poly is not None:
            if user_poly.intersects(country_poly):
                detected_detailed.append(code)
        else:
            # No detailed polygon -- accept bounding-box match only if
            # no detailed polygons matched nearby
            detected_bbox.append(code)

    # Prioritize detailed polygon matches; only include bbox matches if no detailed matches found
    if detected_detailed:
        return sorted(detected_detailed)

    return sorted(detected_bbox)


async def _nominatim_reverse_geocode_with_retry(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    max_retries: int = NOMINATIM_MAX_RETRIES,
) -> Optional[dict]:
    """
    Execute a Nominatim reverse geocode request with retry logic.

    Retries on 429, 502, 503, 504 status codes with exponential backoff.

    Args:
        client: httpx AsyncClient instance
        lat: Latitude
        lon: Longitude
        max_retries: Maximum number of retry attempts

    Returns:
        Response JSON dict on success, or None on failure
    """
    for attempt in range(max_retries):
        try:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "json",
                    "zoom": 3,
                    "addressdetails": 1,
                },
                headers={"User-Agent": "ArmaReforgerMapGenerator/1.0"},
            )

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code in RETRYABLE_STATUS_CODES:
                if attempt < max_retries - 1:
                    wait_time = NOMINATIM_RETRY_WAIT_S * (2 ** attempt)
                    logger.warning(
                        f"Nominatim returned status {resp.status_code} for ({lat}, {lon}), "
                        f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(
                        f"Nominatim failed after {max_retries} retries for ({lat}, {lon}): "
                        f"status {resp.status_code}"
                    )
                    return None
            else:
                logger.warning(
                    f"Nominatim returned non-retryable status {resp.status_code} for ({lat}, {lon})"
                )
                return None

        except Exception as exc:
            if attempt < max_retries - 1:
                wait_time = NOMINATIM_RETRY_WAIT_S * (2 ** attempt)
                logger.warning(
                    f"Nominatim request failed for ({lat}, {lon}): {exc}, "
                    f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(
                    f"Nominatim request failed after {max_retries} retries for ({lat}, {lon}): {exc}"
                )
                return None

    return None


async def _detect_countries_nominatim(polygon_coords: list[list[float]]) -> list[str]:
    """
    Nominatim fallback: reverse-geocode sample points within the polygon.
    Used when polygon-based detection finds no matches, or to refine
    results for countries outside the detailed polygon set.
    """
    from shapely.geometry import Polygon as ShapelyPolygon

    user_poly = ShapelyPolygon([(c[0], c[1]) for c in polygon_coords])
    centroid = user_poly.centroid
    bounds = user_poly.bounds  # (minx, miny, maxx, maxy)

    sample_points = [
        (centroid.x, centroid.y),
        (bounds[0], bounds[1]),
        (bounds[2], bounds[3]),
        (bounds[0], bounds[3]),
        (bounds[2], bounds[1]),
        ((bounds[0] + bounds[2]) / 2, bounds[1]),
        ((bounds[0] + bounds[2]) / 2, bounds[3]),
        (bounds[0], (bounds[1] + bounds[3]) / 2),
        (bounds[2], (bounds[1] + bounds[3]) / 2),
    ]

    countries: set[str] = set()
    async with httpx.AsyncClient(timeout=30) as client:
        for lon, lat in sample_points:
            data = await _nominatim_reverse_geocode_with_retry(client, lat, lon)
            if data:
                cc = data.get("address", {}).get("country_code", "").upper()
                if cc:
                    countries.add(cc)

    return sorted(countries)


def _utm_crs_from_centroid(lng: float, lat: float) -> str:
    """Compute UTM zone EPSG code from centroid coordinates."""
    zone = int((lng + 180) / 6) + 1
    if lat >= 0:
        return f"EPSG:326{zone:02d}"
    else:
        return f"EPSG:327{zone:02d}"


def get_crs_for_area(
    countries: list[str],
    polygon_coords: list[list[float]],
) -> str:
    """
    Determine the best CRS to use for the given area.

    For single-country areas, returns the country's preferred CRS.
    For multi-country areas, picks the country with the most spatial
    overlap.
    """
    if len(countries) == 1:
        return COUNTRY_CRS.get(countries[0], "EPSG:4326")

    # For multi-country areas, pick the country with the most coverage
    user_poly = Polygon([(c[0], c[1]) for c in polygon_coords])
    best_code = None
    best_area = 0.0

    for code in countries:
        country_poly = _get_country_polygon(code)
        if country_poly is None:
            continue
        try:
            intersection = user_poly.intersection(country_poly)
            if intersection.area > best_area:
                best_area = intersection.area
                best_code = code
        except Exception:
            continue

    if best_code:
        return COUNTRY_CRS.get(best_code, "EPSG:4326")

    # Default to UTM zone based on centroid
    centroid_lng = sum(c[0] for c in polygon_coords) / len(polygon_coords)
    centroid_lat = sum(c[1] for c in polygon_coords) / len(polygon_coords)
    return _utm_crs_from_centroid(centroid_lng, centroid_lat)


async def detect_countries(polygon_coords: list[list[float]]) -> dict:
    """
    Main entry point: detect countries for a polygon.

    Uses offline polygon-based detection first, with Nominatim as
    optional fallback for countries not in the polygon set.

    Args:
        polygon_coords: List of [lng, lat] coordinate pairs forming the polygon.

    Returns:
        Dict with:
            - countries: list of ISO 2-letter country codes
            - primary_country: the country containing the polygon centroid
            - crs: recommended CRS for processing
            - bbox: bounding box dict (west, south, east, north)
    """
    polygon = Polygon([(c[0], c[1]) for c in polygon_coords])
    bounds = polygon.bounds  # (minx/west, miny/south, maxx/east, maxy/north)

    # 1. Fast polygon-based detection
    countries = _detect_countries_polygon(polygon_coords)
    logger.info(f"Polygon-based detection: {countries}")

    # 2. If nothing found, try Nominatim as fallback
    if not countries:
        try:
            countries = await _detect_countries_nominatim(polygon_coords)
            logger.info(f"Nominatim fallback detection: {countries}")
        except Exception:
            logger.warning("Nominatim fallback also failed")
            countries = []

    # 3. Determine primary country (country containing centroid)
    primary = countries[0] if countries else "UNKNOWN"
    if len(countries) > 1:
        centroid = polygon.centroid
        centroid_country = _get_country_for_point(centroid.x, centroid.y)
        if centroid_country and centroid_country in countries:
            primary = centroid_country

    # 4. Select CRS
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
    """
    Return the available data sources for a given country.
    Indicates which sources are available and their priority.
    """
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
