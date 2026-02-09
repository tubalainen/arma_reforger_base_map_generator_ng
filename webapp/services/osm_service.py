"""
OpenStreetMap data extraction service via Overpass API.

Extracts roads, water bodies, forests, buildings, and land use
features from OpenStreetMap using the Overpass API.

Uses a pool of public Overpass mirrors for resilience. All mirrors
serve identical OSM data — the pool provides redundancy when
individual instances return 429 (rate-limited) or 504 (timeout).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import httpx

from config import OVERPASS_ENDPOINTS, OVERPASS_TIMEOUT
from services.utils.geo import bbox_to_overpass_str
from services.utils.geojson import (
    extract_coords_from_geometry,
    close_ring,
    extract_outer_rings_from_relation,
    make_polygon_or_multi,
)

logger = logging.getLogger(__name__)


def _polygon_to_overpass_poly(coords: list[list[float]]) -> str:
    """
    Convert polygon coordinates to Overpass poly filter string.
    Coords are [lng, lat] pairs. Overpass wants 'lat lon lat lon ...'
    """
    parts = []
    for lng, lat in coords:
        parts.append(f"{lat} {lng}")
    return " ".join(parts)


def _bbox_to_overpass(bbox: dict) -> str:
    """Convert bbox dict to Overpass bbox string (south,west,north,east)."""
    return bbox_to_overpass_str(bbox)


def _endpoint_label(url: str) -> str:
    """Extract a short human-readable label from an Overpass endpoint URL."""
    if "mail.ru" in url:
        return "VK Maps"
    elif "private.coffee" in url:
        return "Private.coffee"
    elif "kumi" in url:
        return "Kumi"
    elif "overpass-api.de" in url:
        return "overpass-api.de"
    elif "osm.jp" in url:
        return "Japan"
    # Fallback: extract hostname
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or url
    except Exception:
        return url


async def _run_overpass_query(query: str, max_retries: int = 2, job=None) -> Optional[dict]:
    """
    Execute an Overpass API query against a pool of public mirrors.

    Cycles through all configured endpoints on each attempt, moving to
    the next mirror on 429 (rate-limited), 504 (timeout), or connection
    errors. All mirrors serve identical OSM data.

    Args:
        query: Overpass QL query string
        max_retries: Number of full passes through the endpoint pool
    """
    endpoints = OVERPASS_ENDPOINTS

    # Extract query type for logging (first element type in the query)
    query_type = "features"
    if "highway" in query:
        query_type = "roads"
    elif "natural" in query and "water" in query:
        query_type = "water"
    elif "natural" in query and ("wood" in query or "forest" in query):
        query_type = "forests"
    elif "building" in query:
        query_type = "buildings"
    elif "landuse" in query:
        query_type = "land use"

    for attempt in range(max_retries):
        for endpoint_idx, endpoint in enumerate(endpoints):
            label = _endpoint_label(endpoint)
            try:
                logger.debug(
                    f"Querying Overpass [{label}] for {query_type} "
                    f"(endpoint {endpoint_idx + 1}/{len(endpoints)}, "
                    f"attempt {attempt + 1}/{max_retries})"
                )
                async with httpx.AsyncClient(timeout=OVERPASS_TIMEOUT + 30) as client:
                    resp = await client.post(
                        endpoint,
                        data={"data": query},
                        headers={"User-Agent": "ArmaReforgerMapGenerator/1.0"},
                    )
                    if resp.status_code == 200:
                        # Validate that the response is actually JSON before parsing
                        content_type = resp.headers.get("content-type", "").lower()
                        if "application/json" not in content_type:
                            logger.warning(
                                f"Overpass [{label}] returned non-JSON response "
                                f"(Content-Type: {content_type}), trying next..."
                            )
                            logger.debug(f"Response preview: {resp.text[:500]}")
                            continue

                        try:
                            result = resp.json()
                            element_count = len(result.get("elements", []))
                            data_size_kb = len(resp.content) / 1024
                            logger.info(
                                f"Successfully fetched {query_type} from Overpass [{label}]: "
                                f"{element_count} elements, {data_size_kb:.1f} KB"
                            )
                            return result
                        except json.JSONDecodeError as json_err:
                            logger.error(
                                f"Overpass [{label}] returned invalid JSON: {json_err}"
                            )
                            logger.debug(f"Response preview: {resp.text[:500]}")
                            continue
                    elif resp.status_code == 429:
                        logger.warning(f"Overpass [{label}] rate limited (429), trying next...")
                        if job:
                            job.add_log(f"Overpass mirror [{label}] rate limited, trying next...", "warning")
                        continue
                    elif resp.status_code == 504:
                        logger.warning(f"Overpass [{label}] timeout (504), trying next...")
                        if job:
                            job.add_log(f"Overpass mirror [{label}] timed out, trying next...", "warning")
                        continue
                    else:
                        logger.error(f"Overpass [{label}] error {resp.status_code}: {resp.text[:300]}")
                        continue
            except Exception as e:
                logger.error(f"Overpass [{label}] request failed: {e}")
                continue

        # All endpoints failed on this attempt; wait before retrying
        if attempt < max_retries - 1:
            wait = 5  # Fixed 5 second wait instead of increasing wait time
            logger.warning(
                f"All {len(endpoints)} Overpass endpoints failed for {query_type}, "
                f"retrying in {wait}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(wait)

    logger.error("All Overpass endpoints failed after all retries - continuing with partial data")
    return None


async def fetch_roads(bbox: dict, job = None) -> Optional[dict]:
    """
    Fetch road network from OSM.
    Returns all highway features with classification, surface, width, etc.
    """
    bbox_str = _bbox_to_overpass(bbox)
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}][bbox:{bbox_str}];
    (
      way["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|residential|unclassified|service|track|path|footway|cycleway|bridleway|living_street)$"];
    );
    out body geom;
    """

    logger.info(f"Fetching roads from Overpass API (bbox: {bbox_str})...")
    result = await _run_overpass_query(query, job=job)

    if result and "elements" in result:
        roads = _process_road_elements(result["elements"])
        # Count road types
        road_types = {}
        for road in roads:
            highway_type = road["properties"].get("highway", "unknown")
            road_types[highway_type] = road_types.get(highway_type, 0) + 1

        top_types = dict(sorted(road_types.items(), key=lambda x: x[1], reverse=True)[:5])
        logger.info(f"Fetched {len(roads)} road segments across {len(road_types)} types: {top_types}")

        if job:
            # Format road type breakdown for activity log
            type_details = ", ".join([f"{k}: {v}" for k, v in list(top_types.items())[:5]])
            job.add_log(f"✓ Roads: {len(roads)} segments ({type_details})", "success")

        return {"type": "FeatureCollection", "features": roads}

    logger.warning("No road data returned from Overpass API")
    return None


def _process_road_elements(elements: list) -> list:
    """Convert Overpass road elements to GeoJSON features."""
    features = []
    for elem in elements:
        if elem.get("type") != "way" or "geometry" not in elem:
            continue

        tags = elem.get("tags", {})
        highway_type = tags.get("highway", "unclassified")
        surface = tags.get("surface", "")
        width = tags.get("width", "")
        name = tags.get("name", "")
        bridge = tags.get("bridge", "no")
        tunnel = tags.get("tunnel", "no")
        lanes = tags.get("lanes", "")

        coords = extract_coords_from_geometry(elem["geometry"])

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "osm_id": elem["id"],
                "highway": highway_type,
                "surface": surface,
                "width": width,
                "name": name,
                "bridge": bridge,
                "tunnel": tunnel,
                "lanes": lanes,
            },
        }
        features.append(feature)

    return features


async def fetch_water(bbox: dict, job = None) -> Optional[dict]:
    """
    Fetch water features from OSM.
    Includes lakes, rivers, streams, ponds, reservoirs, coastline.
    """
    bbox_str = _bbox_to_overpass(bbox)
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}][bbox:{bbox_str}];
    (
      // Lakes, ponds, reservoirs (polygons)
      way["natural"="water"];
      relation["natural"="water"];
      // Rivers and streams (lines)
      way["waterway"~"^(river|stream|canal|ditch|drain)$"];
      // Coastline
      way["natural"="coastline"];
      // Wetlands
      way["natural"="wetland"];
      relation["natural"="wetland"];
    );
    out body geom;
    """

    logger.info(f"Fetching water features from Overpass API (bbox: {bbox_str})...")
    result = await _run_overpass_query(query, job=job)

    if result and "elements" in result:
        features = _process_water_elements(result["elements"])
        # Count water types
        water_types = {}
        for feat in features:
            water_type = feat["properties"].get("water_type", "unknown")
            water_types[water_type] = water_types.get(water_type, 0) + 1

        logger.info(f"Fetched {len(features)} water features: {water_types}")

        if job:
            # Format water type breakdown for activity log
            type_details = ", ".join([f"{k}: {v}" for k, v in water_types.items()])
            job.add_log(f"✓ Water: {len(features)} features ({type_details})", "success")

        return {"type": "FeatureCollection", "features": features}

    logger.warning("No water data returned from Overpass API")
    return None


def _process_water_elements(elements: list) -> list:
    """Convert Overpass water elements to GeoJSON features."""
    features = []
    for elem in elements:
        tags = elem.get("tags", {})

        if elem.get("type") == "way" and "geometry" in elem:
            coords = extract_coords_from_geometry(elem["geometry"])

            is_area = (
                tags.get("natural") in ("water", "wetland")
                and len(coords) > 3
                and coords[0] == coords[-1]
            )

            water_type = "unknown"
            if tags.get("natural") == "water":
                water_type = tags.get("water", "lake")
            elif tags.get("waterway"):
                water_type = tags["waterway"]
            elif tags.get("natural") == "coastline":
                water_type = "coastline"
            elif tags.get("natural") == "wetland":
                water_type = "wetland"

            geom_type = "Polygon" if is_area else "LineString"
            geom_coords = [coords] if is_area else coords

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": geom_type,
                    "coordinates": geom_coords,
                },
                "properties": {
                    "osm_id": elem["id"],
                    "water_type": water_type,
                    "name": tags.get("name", ""),
                    "natural": tags.get("natural", ""),
                    "waterway": tags.get("waterway", ""),
                    "intermittent": tags.get("intermittent", "no"),
                },
            }
            features.append(feature)

        elif elem.get("type") == "relation" and "members" in elem:
            outer_rings = extract_outer_rings_from_relation(elem)

            if outer_rings:
                water_type = tags.get("water", tags.get("natural", "water"))
                feature = {
                    "type": "Feature",
                    "geometry": make_polygon_or_multi(outer_rings),
                    "properties": {
                        "osm_id": elem["id"],
                        "water_type": water_type,
                        "name": tags.get("name", ""),
                        "natural": tags.get("natural", ""),
                    },
                }
                features.append(feature)

    return features


async def fetch_forests(bbox: dict, job = None) -> Optional[dict]:
    """
    Fetch forest and woodland areas from OSM.
    Includes forest, wood, scrub, and tree rows.
    """
    bbox_str = _bbox_to_overpass(bbox)
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}][bbox:{bbox_str}];
    (
      way["natural"="wood"];
      relation["natural"="wood"];
      way["landuse"="forest"];
      relation["landuse"="forest"];
      way["natural"="scrub"];
      way["natural"="heath"];
      way["natural"="tree_row"];
    );
    out body geom;
    """

    logger.info(f"Fetching forests from Overpass API (bbox: {bbox_str})...")
    result = await _run_overpass_query(query, job=job)

    if result and "elements" in result:
        features = _process_area_elements(result["elements"], "forest")
        # Count forest types
        forest_types = {}
        for feat in features:
            area_type = feat["properties"].get("type", "unknown")
            forest_types[area_type] = forest_types.get(area_type, 0) + 1

        logger.info(f"Fetched {len(features)} forest/woodland features: {forest_types}")

        if job:
            # Format forest type breakdown for activity log
            type_details = ", ".join([f"{k}: {v}" for k, v in forest_types.items()])
            job.add_log(f"✓ Forests: {len(features)} areas ({type_details})", "success")

        return {"type": "FeatureCollection", "features": features}

    logger.warning("No forest data returned from Overpass API")
    return None


async def fetch_buildings(bbox: dict, job = None) -> Optional[dict]:
    """
    Fetch building footprints from OSM.
    Includes building type, height, levels.
    """
    bbox_str = _bbox_to_overpass(bbox)
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}][bbox:{bbox_str}];
    (
      way["building"];
      relation["building"];
    );
    out body geom;
    """

    logger.info(f"Fetching buildings from Overpass API (bbox: {bbox_str})...")
    result = await _run_overpass_query(query, job=job)

    if result and "elements" in result:
        features = _process_building_elements(result["elements"])
        # Count building types
        building_types = {}
        for feat in features:
            bld_type = feat["properties"].get("building_type", "unknown")
            building_types[bld_type] = building_types.get(bld_type, 0) + 1

        top_types = dict(sorted(building_types.items(), key=lambda x: x[1], reverse=True)[:5])
        logger.info(f"Fetched {len(features)} building footprints. Top types: {top_types}")

        if job:
            # Format building type breakdown for activity log
            type_details = ", ".join([f"{k}: {v}" for k, v in list(top_types.items())[:5]])
            job.add_log(f"✓ Buildings: {len(features)} structures ({type_details})", "success")

        return {"type": "FeatureCollection", "features": features}

    logger.warning("No building data returned from Overpass API")
    return None


def _process_building_elements(elements: list) -> list:
    """Convert Overpass building elements to GeoJSON features."""
    features = []
    for elem in elements:
        if elem.get("type") != "way" or "geometry" not in elem:
            continue

        tags = elem.get("tags", {})
        coords = extract_coords_from_geometry(elem["geometry"])

        if len(coords) < 4:
            continue

        close_ring(coords)

        height = 0
        if "height" in tags:
            try:
                height = float(tags["height"].replace("m", "").strip())
            except ValueError:
                pass
        elif "building:levels" in tags:
            try:
                height = int(tags["building:levels"]) * 3
            except ValueError:
                pass

        building_type = tags.get("building", "yes")
        if building_type == "yes":
            if tags.get("amenity") == "place_of_worship":
                building_type = "church"
            elif tags.get("shop"):
                building_type = "commercial"
            elif tags.get("office"):
                building_type = "office"

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [coords],
            },
            "properties": {
                "osm_id": elem["id"],
                "building_type": building_type,
                "height": height,
                "levels": tags.get("building:levels", ""),
                "name": tags.get("name", ""),
                "material": tags.get("building:material", ""),
                "roof_shape": tags.get("roof:shape", ""),
            },
        }
        features.append(feature)

    return features


async def fetch_land_use(bbox: dict, job = None) -> Optional[dict]:
    """
    Fetch land use areas from OSM.
    Includes farmland, meadow, residential, industrial, commercial, etc.
    """
    bbox_str = _bbox_to_overpass(bbox)
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}][bbox:{bbox_str}];
    (
      way["landuse"~"^(farmland|meadow|orchard|vineyard|residential|industrial|commercial|retail|quarry|cemetery|allotments|recreation_ground|military|farmyard)$"];
      relation["landuse"~"^(farmland|meadow|orchard|vineyard|residential|industrial|commercial|retail|quarry|cemetery|allotments|recreation_ground|military|farmyard)$"];
      way["leisure"~"^(park|garden|pitch|playground|golf_course)$"];
      way["natural"~"^(beach|sand|bare_rock|scree|grassland|fell)$"];
    );
    out body geom;
    """

    logger.info(f"Fetching land use from Overpass API (bbox: {bbox_str})...")
    result = await _run_overpass_query(query, job=job)

    if result and "elements" in result:
        features = _process_area_elements(result["elements"], "land_use")
        # Count land use types
        land_use_types = {}
        for feat in features:
            lu_type = feat["properties"].get("type", "unknown")
            land_use_types[lu_type] = land_use_types.get(lu_type, 0) + 1

        top_types = dict(sorted(land_use_types.items(), key=lambda x: x[1], reverse=True)[:5])
        logger.info(f"Fetched {len(features)} land use features. Top types: {top_types}")

        if job:
            # Format land use type breakdown for activity log
            type_details = ", ".join([f"{k}: {v}" for k, v in list(top_types.items())[:5]])
            job.add_log(f"✓ Land use: {len(features)} areas ({type_details})", "success")

        return {"type": "FeatureCollection", "features": features}

    logger.warning("No land use data returned from Overpass API")
    return None


def _process_area_elements(elements: list, category: str) -> list:
    """Generic processor for area elements (forests, land use, etc.)."""
    features = []
    for elem in elements:
        tags = elem.get("tags", {})

        if elem.get("type") == "way" and "geometry" in elem:
            coords = extract_coords_from_geometry(elem["geometry"])
            if len(coords) < 4:
                continue

            close_ring(coords)

            area_type = (
                tags.get("landuse", "")
                or tags.get("natural", "")
                or tags.get("leisure", "")
                or "unknown"
            )

            leaf_type = tags.get("leaf_type", "")
            wood_type = tags.get("wood", "")

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords],
                },
                "properties": {
                    "osm_id": elem["id"],
                    "category": category,
                    "type": area_type,
                    "leaf_type": leaf_type,
                    "wood_type": wood_type,
                    "name": tags.get("name", ""),
                },
            }
            features.append(feature)

        elif elem.get("type") == "relation" and "members" in elem:
            outer_rings = extract_outer_rings_from_relation(elem)

            if outer_rings:
                area_type = (
                    tags.get("landuse", "")
                    or tags.get("natural", "")
                    or tags.get("leisure", "")
                    or "unknown"
                )

                feature = {
                    "type": "Feature",
                    "geometry": make_polygon_or_multi(outer_rings),
                    "properties": {
                        "osm_id": elem["id"],
                        "category": category,
                        "type": area_type,
                        "leaf_type": tags.get("leaf_type", ""),
                        "name": tags.get("name", ""),
                    },
                }
                features.append(feature)

    return features


async def fetch_all_features(bbox: dict, job = None) -> dict:
    """
    Fetch all OSM features for a bounding box.
    Returns dict with roads, water, forests, buildings, land_use collections.

    Runs all 5 queries concurrently with asyncio.gather.
    """
    if job:
        job.add_log("Fetching roads from OpenStreetMap...")
        job.progress = 27
    roads_task = fetch_roads(bbox, job)
    if job:
        job.add_log("Fetching water features from OpenStreetMap...")
        job.progress = 29
    water_task = fetch_water(bbox, job)
    if job:
        job.add_log("Fetching forests from OpenStreetMap...")
        job.progress = 31
    forests_task = fetch_forests(bbox, job)
    if job:
        job.add_log("Fetching buildings from OpenStreetMap...")
        job.progress = 33
    buildings_task = fetch_buildings(bbox, job)
    if job:
        job.add_log("Fetching land use data from OpenStreetMap...")
        job.progress = 35
    land_use_task = fetch_land_use(bbox, job)

    roads, water, forests, buildings, land_use = await asyncio.gather(
        roads_task, water_task, forests_task, buildings_task, land_use_task,
        return_exceptions=True,
    )

    def _safe_result(result, name):
        if isinstance(result, Exception):
            logger.error(f"Failed to fetch {name}: {result}")
            if job:
                job.add_log(f"Warning: Failed to fetch {name}: {result}", "warning")
            return {"type": "FeatureCollection", "features": []}
        return result or {"type": "FeatureCollection", "features": []}

    result = {
        "roads": _safe_result(roads, "roads"),
        "water": _safe_result(water, "water"),
        "forests": _safe_result(forests, "forests"),
        "buildings": _safe_result(buildings, "buildings"),
        "land_use": _safe_result(land_use, "land_use"),
    }

    if job:
        counts = {k: len(v.get("features", [])) for k, v in result.items()}
        job.add_log(
            f"Fetched {counts['roads']} roads, {counts['water']} water features, "
            f"{counts['forests']} forests, {counts['buildings']} buildings, "
            f"{counts['land_use']} land use areas",
            "success"
        )

    return result
