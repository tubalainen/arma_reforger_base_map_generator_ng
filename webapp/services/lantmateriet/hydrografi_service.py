"""
Lantmäteriet Hydrografi (water features) service.

Fetches official Swedish water data from the OGC API Features Hydrografi API
and translates it into the existing osm_service water GeoJSON schema so it
can slot directly into the pipeline without changing downstream consumers.

Collections used:
- StandingWater (391K) — lakes, ponds, reservoirs (Polygon)
- WatercourseLine (924K) — rivers, streams, canals (LineString)
- WatercoursePolygon (27K) — wide rivers/canals (Polygon)
- Wetland (1.4M) — wetlands, bogs (Polygon)

All collections use INSPIRE-compliant property names (inspireId, localType,
persistence, geographicalName, etc.).

Authentication: Basic Auth (same credentials as STAC Höjd).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config.lantmateriet import LANTMATERIET_CONFIG
from services.lantmateriet.ogc_features_client import fetch_ogc_collection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# localType → water_type mapping
# ---------------------------------------------------------------------------
# These are INSPIRE localType values used by Lantmäteriet.
# Unknown values are logged at INFO and assigned a default.

_STANDING_WATER_TYPE_MAP: dict[str, str] = {
    # Swedish localType values (may appear in Swedish or English)
    "Sjö": "lake",
    "Lake": "lake",
    "Damm": "pond",
    "Pond": "pond",
    "Reservoar": "reservoir",
    "Reservoir": "reservoir",
    "Magasin": "reservoir",
    "Bassäng": "pond",
}

_WATERCOURSE_TYPE_MAP: dict[str, str] = {
    # Rivers (larger watercourses)
    "Flod": "river",
    "River": "river",
    "Å": "river",
    "Älv": "river",
    # Canals
    "Kanal": "canal",
    "Canal": "canal",
    # Streams (smaller watercourses) — default
    "Bäck": "stream",
    "Stream": "stream",
    "Dike": "ditch",
    "Ditch": "ditch",
}


def _extract_name(feature: dict) -> str:
    """
    Extract feature name from INSPIRE geographicalName structure.

    The geographicalName field can be:
    - An array of objects: [{"text": "Lake Name", "language": "swe", ...}]
    - An array of strings (simplified): ["Lake Name"]
    - Missing/null
    """
    props = feature.get("properties", {})

    # Try geographicalName array
    geo_names = props.get("geographicalName", None)
    if geo_names and isinstance(geo_names, list):
        first = geo_names[0]
        if isinstance(first, dict):
            return first.get("text", "")
        if isinstance(first, str):
            return first

    # Try nested geographicalName.text array
    name_texts = props.get("geographicalName.text", None)
    if name_texts and isinstance(name_texts, list) and name_texts[0]:
        return name_texts[0]

    # Try simple name field
    return props.get("name", "")


def _extract_persistence(feature: dict) -> str:
    """Map INSPIRE persistence to OSM intermittent yes/no."""
    persistence = (feature.get("properties", {})
                   .get("persistence", "")
                   .lower())
    if persistence in ("intermittent", "ephemeral"):
        return "yes"
    return "no"


def _feature_id(feature: dict) -> int:
    """Extract a numeric ID for compatibility with osm_id field."""
    # Try top-level id first (most common in OGC Features)
    fid = feature.get("id")
    if fid is not None:
        try:
            return int(fid)
        except (ValueError, TypeError):
            pass

    # Try inspireId from properties
    inspire_id = feature.get("properties", {}).get("inspireId", "")
    if inspire_id:
        # Hash it to get a stable numeric ID
        return abs(hash(inspire_id)) % (10 ** 10)

    # Fallback: Id property
    prop_id = feature.get("properties", {}).get("Id")
    if prop_id is not None:
        try:
            return int(prop_id)
        except (ValueError, TypeError):
            pass

    return 0


# ---------------------------------------------------------------------------
# Translation functions (raw INSPIRE → OSM-compatible schema)
# ---------------------------------------------------------------------------

def _translate_standing_water(features: list[dict]) -> list[dict]:
    """Translate StandingWater features to OSM water schema."""
    translated = []
    unknown_types: dict[str, int] = {}

    for f in features:
        props = f.get("properties", {})
        local_type = props.get("localType", "")

        water_type = _STANDING_WATER_TYPE_MAP.get(local_type, None)
        if water_type is None:
            water_type = "lake"  # Default for standing water
            if local_type:
                unknown_types[local_type] = unknown_types.get(local_type, 0) + 1

        translated.append({
            "type": "Feature",
            "geometry": f.get("geometry", {}),
            "properties": {
                "osm_id": _feature_id(f),
                "water_type": water_type,
                "name": _extract_name(f),
                "natural": "water",
                "waterway": "",
                "intermittent": _extract_persistence(f),
            },
        })

    if unknown_types:
        logger.info(
            f"StandingWater: unknown localType values (defaulted to 'lake'): "
            f"{unknown_types}"
        )

    return translated


def _translate_watercourse_line(features: list[dict]) -> list[dict]:
    """Translate WatercourseLine features to OSM water schema."""
    translated = []
    unknown_types: dict[str, int] = {}

    for f in features:
        props = f.get("properties", {})
        local_type = props.get("localType", "")

        water_type = _WATERCOURSE_TYPE_MAP.get(local_type, None)
        if water_type is None:
            water_type = "stream"  # Default for watercourse lines
            if local_type:
                unknown_types[local_type] = unknown_types.get(local_type, 0) + 1

        translated.append({
            "type": "Feature",
            "geometry": f.get("geometry", {}),
            "properties": {
                "osm_id": _feature_id(f),
                "water_type": water_type,
                "name": _extract_name(f),
                "natural": "",
                "waterway": water_type,  # river/stream/canal/ditch
                "intermittent": _extract_persistence(f),
            },
        })

    if unknown_types:
        logger.info(
            f"WatercourseLine: unknown localType values (defaulted to 'stream'): "
            f"{unknown_types}"
        )

    return translated


def _translate_watercourse_polygon(features: list[dict]) -> list[dict]:
    """Translate WatercoursePolygon features to OSM water schema."""
    return [
        {
            "type": "Feature",
            "geometry": f.get("geometry", {}),
            "properties": {
                "osm_id": _feature_id(f),
                "water_type": "river",
                "name": _extract_name(f),
                "natural": "water",
                "waterway": "",
                "intermittent": _extract_persistence(f),
            },
        }
        for f in features
    ]


def _translate_wetland(features: list[dict]) -> list[dict]:
    """Translate Wetland features to OSM water schema."""
    return [
        {
            "type": "Feature",
            "geometry": f.get("geometry", {}),
            "properties": {
                "osm_id": _feature_id(f),
                "water_type": "wetland",
                "name": _extract_name(f),
                "natural": "wetland",
                "waterway": "",
                "intermittent": "no",
            },
        }
        for f in features
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_lantmateriet_water(
    bbox_wgs84: tuple[float, float, float, float],
    job=None,
) -> Optional[dict]:
    """
    Fetch water features from Lantmäteriet Hydrografi API.

    Queries StandingWater, WatercourseLine, WatercoursePolygon, and Wetland
    collections concurrently and translates them to the OSM-compatible
    water GeoJSON schema used by the rest of the pipeline.

    Args:
        bbox_wgs84: Bounding box as (west, south, east, north) in WGS84
        job: Optional MapGenerationJob for progress logging

    Returns:
        GeoJSON FeatureCollection matching osm_service water schema,
        or None if all collections fail (triggers OSM fallback).
    """
    base_url = LANTMATERIET_CONFIG.hydrografi_endpoint

    if job:
        job.add_log("Fetching water features from Lantmäteriet Hydrografi...")

    # Fetch all collections concurrently
    standing_task = fetch_ogc_collection(
        base_url, "StandingWater", bbox_wgs84, job=job
    )
    line_task = fetch_ogc_collection(
        base_url, "WatercourseLine", bbox_wgs84, job=job
    )
    polygon_task = fetch_ogc_collection(
        base_url, "WatercoursePolygon", bbox_wgs84, job=job
    )
    wetland_task = fetch_ogc_collection(
        base_url, "Wetland", bbox_wgs84, job=job
    )

    results = await asyncio.gather(
        standing_task, line_task, polygon_task, wetland_task,
        return_exceptions=True,
    )

    standing_raw, line_raw, polygon_raw, wetland_raw = results

    # Translate each collection (skip on failure/None)
    all_features: list[dict] = []

    if isinstance(standing_raw, list) and standing_raw:
        translated = _translate_standing_water(standing_raw)
        all_features.extend(translated)
        logger.info(f"Hydrografi StandingWater: {len(translated)} features")

    if isinstance(line_raw, list) and line_raw:
        translated = _translate_watercourse_line(line_raw)
        all_features.extend(translated)
        logger.info(f"Hydrografi WatercourseLine: {len(translated)} features")

    if isinstance(polygon_raw, list) and polygon_raw:
        translated = _translate_watercourse_polygon(polygon_raw)
        all_features.extend(translated)
        logger.info(f"Hydrografi WatercoursePolygon: {len(translated)} features")

    if isinstance(wetland_raw, list) and wetland_raw:
        translated = _translate_wetland(wetland_raw)
        all_features.extend(translated)
        logger.info(f"Hydrografi Wetland: {len(translated)} features")

    if not all_features:
        # All collections failed or returned empty
        logger.warning("No water features from Lantmäteriet Hydrografi")
        return None

    if job:
        job.add_log(
            f"✓ Lantmäteriet water: {len(all_features)} features "
            f"(lakes, rivers, wetlands)",
            "success",
        )

    return {"type": "FeatureCollection", "features": all_features}
