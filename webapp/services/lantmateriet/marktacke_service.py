"""
Lantmäteriet Marktäcke (land cover) service.

Fetches official Swedish land cover classification from the OGC API Features
Marktäcke API and translates it into the existing osm_service GeoJSON schemas
(forests, land_use, supplementary water) so it can slot directly into the
pipeline without changing downstream consumers.

Collections used:
- Markytor (3.6M features) — main land cover polygons
- Sankmarksytor (1.4M features) — wetland subtype polygons

Each feature has an objekttypnr (integer code) and objekttyp (Swedish name)
that classifies it into one of 18 land cover types.

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
# Marktäcke objekttypnr → pipeline mapping
# ---------------------------------------------------------------------------
# Each entry: (target_osm_data_key, mapped_properties)
#
# target keys: "forests", "land_use", "water", or None (skip)

_OBJEKTTYP_MAP: dict[int, dict] = {
    # --- Water ---
    2631: {
        "target": "water",
        "props": {
            "water_type": "coastline",
            "natural": "water",
            "waterway": "",
            "intermittent": "no",
        },
    },
    2632: {
        "target": "water",
        "props": {
            "water_type": "lake",
            "natural": "water",
            "waterway": "",
            "intermittent": "no",
        },
    },
    2633: {
        "target": "water",
        "props": {
            "water_type": "river",
            "natural": "water",
            "waterway": "",
            "intermittent": "no",
        },
    },
    2634: {
        "target": "water",
        "props": {
            "water_type": "reservoir",
            "natural": "water",
            "waterway": "",
            "intermittent": "no",
        },
    },
    # --- Special / Mountain ---
    2635: {
        "target": "land_use",
        "props": {
            "category": "natural",
            "type": "bare_rock",
            "leaf_type": "",
        },
    },
    # --- Built-up areas ---
    2636: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "residential",
            "leaf_type": "",
        },
    },
    2637: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "residential",
            "leaf_type": "",
        },
    },
    2638: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "residential",
            "leaf_type": "",
        },
    },
    2639: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "industrial",
            "leaf_type": "",
        },
    },
    # --- Open land ---
    2640: {
        "target": "land_use",
        "props": {
            "category": "natural",
            "type": "grassland",
            "leaf_type": "",
        },
    },
    2641: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "retail",
            "leaf_type": "",
        },
    },
    # --- Agriculture ---
    2642: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "farmland",
            "leaf_type": "",
        },
    },
    2643: {
        "target": "land_use",
        "props": {
            "category": "landuse",
            "type": "orchard",
            "leaf_type": "",
        },
    },
    # --- Alpine barren ---
    2644: {
        "target": "land_use",
        "props": {
            "category": "natural",
            "type": "bare_rock",
            "leaf_type": "",
        },
    },
    # --- Forest ---
    2645: {
        "target": "forests",
        "props": {
            "category": "forest",
            "type": "forest",
            "leaf_type": "needleleaved",
        },
    },
    2646: {
        "target": "forests",
        "props": {
            "category": "forest",
            "type": "forest",
            "leaf_type": "broadleaved",
        },
    },
    2647: {
        "target": "forests",
        "props": {
            "category": "forest",
            "type": "forest",
            "leaf_type": "broadleaved",
        },
    },
    # --- Unmapped ---
    2648: None,  # Ej karterat område — skip
}

# Human-readable names for logging
_OBJEKTTYP_NAMES: dict[int, str] = {
    2631: "Hav",
    2632: "Sjö",
    2633: "Vattendragsyta",
    2634: "Anlagt vatten",
    2635: "Glaciär",
    2636: "Sluten bebyggelse",
    2637: "Hög bebyggelse",
    2638: "Låg bebyggelse",
    2639: "Industri/handelsbebyggelse",
    2640: "Öppen mark",
    2641: "Torg",
    2642: "Åker",
    2643: "Fruktodling",
    2644: "Kalfjäll",
    2645: "Barr- och blandskog",
    2646: "Lövskog",
    2647: "Fjällbjörkskog",
    2648: "Ej karterat område",
}


def _feature_id(feature: dict) -> str:
    """Extract an ID string for compatibility with osm_id field."""
    # Try top-level id
    fid = feature.get("id")
    if fid is not None:
        return str(fid)

    # Try objektidentitet from properties
    oid = feature.get("properties", {}).get("objektidentitet", "")
    if oid:
        return oid

    return "0"


def _translate_markytor(
    features: list[dict],
) -> dict[str, list[dict]]:
    """
    Translate Markytor features into forests/land_use/water buckets.

    Returns dict with keys "forests", "land_use", "water" — each
    containing a list of GeoJSON features matching the OSM schema.
    """
    buckets: dict[str, list[dict]] = {
        "forests": [],
        "land_use": [],
        "water": [],
    }

    type_counts: dict[str, int] = {}
    unknown_types: dict[int, int] = {}

    for f in features:
        props = f.get("properties", {})
        objekttypnr = props.get("objekttypnr")

        if objekttypnr is None:
            continue

        # Try integer conversion (API may return string)
        try:
            objekttypnr = int(objekttypnr)
        except (ValueError, TypeError):
            continue

        mapping = _OBJEKTTYP_MAP.get(objekttypnr)

        if mapping is None:
            # Skip unmapped types (2648 = Ej karterat)
            if objekttypnr != 2648:
                unknown_types[objekttypnr] = (
                    unknown_types.get(objekttypnr, 0) + 1
                )
            continue

        target = mapping["target"]
        mapped_props = mapping["props"]

        # Build translated feature
        translated = {
            "type": "Feature",
            "geometry": f.get("geometry", {}),
            "properties": {
                "osm_id": _feature_id(f),
                "name": "",
                **mapped_props,
            },
        }

        # For water features, also add the name if no name field set
        if target == "water":
            translated["properties"].setdefault("waterway", "")
            translated["properties"].setdefault("intermittent", "no")
            translated["properties"].setdefault("natural", "water")

        # For forests, ensure wood_type is present
        if target == "forests":
            translated["properties"].setdefault("wood_type", "")

        buckets[target].append(translated)

        # Count for logging
        type_name = _OBJEKTTYP_NAMES.get(objekttypnr, str(objekttypnr))
        type_counts[type_name] = type_counts.get(type_name, 0) + 1

    if unknown_types:
        logger.info(
            f"Markytor: unknown objekttypnr values (skipped): {unknown_types}"
        )

    if type_counts:
        logger.info(f"Markytor type breakdown: {type_counts}")

    return buckets


def _translate_sankmarksytor(features: list[dict]) -> list[dict]:
    """
    Translate Sankmarksytor (wetland) features to OSM water schema.

    All wetland features map to water_type="wetland".
    """
    return [
        {
            "type": "Feature",
            "geometry": f.get("geometry", {}),
            "properties": {
                "osm_id": _feature_id(f),
                "water_type": "wetland",
                "name": "",
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

async def fetch_lantmateriet_land_cover(
    bbox_wgs84: tuple[float, float, float, float],
    job=None,
) -> Optional[dict]:
    """
    Fetch land cover from Lantmäteriet Marktäcke API.

    Queries Markytor and Sankmarksytor collections concurrently, then
    splits results into forests, land_use, and supplementary water
    FeatureCollections matching the existing OSM-based pipeline schemas.

    Args:
        bbox_wgs84: Bounding box as (west, south, east, north) in WGS84
        job: Optional MapGenerationJob for progress logging

    Returns:
        Dict with three FeatureCollections:
        {
            "forests": {...},    # matches osm_data["forests"] schema
            "land_use": {...},   # matches osm_data["land_use"] schema
            "water": {...},      # supplementary water from Marktäcke
        }
        Or None if both collections fail (triggers OSM fallback).
    """
    base_url = LANTMATERIET_CONFIG.marktacke_endpoint

    if job:
        job.add_log(
            "Fetching land cover from Lantmäteriet Marktäcke..."
        )

    # Fetch both collections concurrently
    markytor_task = fetch_ogc_collection(
        base_url, "markytor", bbox_wgs84, job=job
    )
    sankmark_task = fetch_ogc_collection(
        base_url, "sankmarksytor", bbox_wgs84, job=job
    )

    results = await asyncio.gather(
        markytor_task, sankmark_task,
        return_exceptions=True,
    )

    markytor_raw, sankmark_raw = results

    # Translate Markytor
    forests: list[dict] = []
    land_use: list[dict] = []
    water: list[dict] = []

    if isinstance(markytor_raw, list) and markytor_raw:
        buckets = _translate_markytor(markytor_raw)
        forests.extend(buckets["forests"])
        land_use.extend(buckets["land_use"])
        water.extend(buckets["water"])
        logger.info(
            f"Marktäcke Markytor: {len(forests)} forests, "
            f"{len(land_use)} land_use, {len(water)} water"
        )
    elif isinstance(markytor_raw, Exception):
        logger.error(f"Markytor fetch failed: {markytor_raw}")
    else:
        logger.warning("No features from Marktäcke Markytor")

    # Translate Sankmarksytor (wetlands → water)
    if isinstance(sankmark_raw, list) and sankmark_raw:
        wetlands = _translate_sankmarksytor(sankmark_raw)
        water.extend(wetlands)
        logger.info(f"Marktäcke Sankmarksytor: {len(wetlands)} wetland features")
    elif isinstance(sankmark_raw, Exception):
        logger.error(f"Sankmarksytor fetch failed: {sankmark_raw}")

    # Check if we got anything at all
    if not forests and not land_use and not water:
        logger.warning("No land cover data from Lantmäteriet Marktäcke")
        return None

    if job:
        job.add_log(
            f"✓ Lantmäteriet land cover: {len(forests)} forest areas, "
            f"{len(land_use)} land use areas, {len(water)} water features",
            "success",
        )

    return {
        "forests": {"type": "FeatureCollection", "features": forests},
        "land_use": {"type": "FeatureCollection", "features": land_use},
        "water": {"type": "FeatureCollection", "features": water},
    }
