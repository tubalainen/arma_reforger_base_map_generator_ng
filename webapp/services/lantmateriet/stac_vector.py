"""
Lantmäteriet STAC Vektor (vector features) service.

Fetches official Swedish vector features (roads, water, buildings, land use)
from the STAC Vektor API. Returns data as GeoJSON-compatible dicts
(FeatureCollections) so they can slot directly into the existing osm_service
pipeline without changing downstream consumers.

Authentication: Basic auth (username/password).
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import httpx

from config.lantmateriet import LANTMATERIET_CONFIG
from services.lantmateriet.auth import get_authenticated_headers

logger = logging.getLogger(__name__)

# Map feature types to expected STAC collection IDs.
# These are provisional — adjust based on actual STAC Vektor API response.
_COLLECTION_MAP = {
    "roads": "roads",
    "water": "hydrography",
    "buildings": "buildings",
    "landuse": "landuse",
}


async def fetch_stac_vector_features(
    bbox_wgs84: tuple[float, float, float, float],
    feature_type: str,
) -> Optional[dict]:
    """
    Fetch vector features from Lantmäteriet STAC Vektor API.

    Returns data as a GeoJSON-compatible dict (FeatureCollection)
    so it can slot directly into the existing osm_service pipeline
    without changing downstream consumers.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84
        feature_type: Feature type ("roads", "water", "buildings", "landuse")

    Returns:
        GeoJSON FeatureCollection dict or None on failure.
    """
    headers = get_authenticated_headers()
    if "Authorization" not in headers:
        logger.error("Cannot fetch STAC vectors: no authentication credentials")
        return None

    search_url = f"{LANTMATERIET_CONFIG.stac_vektor_endpoint}/search"

    collection = _COLLECTION_MAP.get(feature_type)
    if not collection:
        logger.warning(f"Unknown Lantmäteriet feature type: {feature_type}")
        return None

    query = {
        "bbox": list(bbox_wgs84),
        "collections": [collection],
        "limit": 1000,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(search_url, json=query, headers=headers)
            resp.raise_for_status()

            stac_result = resp.json()
            features = stac_result.get("features", [])

            if not features:
                logger.info(f"No {feature_type} features from Lantmäteriet STAC")
                return None

            # Download and merge GeoJSON/GeoPackage assets
            import geopandas as gpd
            import pandas as pd

            all_gdfs = []
            for feature in features:
                assets = feature.get("assets", {})
                asset = (
                    assets.get("geojson")
                    or assets.get("gpkg")
                    or next(iter(assets.values()), None)
                )
                if asset is None:
                    continue

                asset_url = asset["href"]
                data_resp = await client.get(asset_url, headers=headers)
                data_resp.raise_for_status()

                # Parse into GeoDataFrame
                gdf = gpd.read_file(io.BytesIO(data_resp.content))
                all_gdfs.append(gdf)

            if not all_gdfs:
                return None

            combined = gpd.GeoDataFrame(
                pd.concat(all_gdfs, ignore_index=True)
            )
            logger.info(
                f"Fetched {len(combined)} {feature_type} features from Lantmäteriet"
            )

            # Convert to GeoJSON FeatureCollection dict for compatibility
            # with existing osm_service consumers (road_processor, feature_extractor)
            return {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": row.geometry.__geo_interface__,
                        "properties": {
                            k: v for k, v in row.items() if k != "geometry"
                        },
                    }
                    for _, row in combined.iterrows()
                ],
            }

    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error fetching STAC {feature_type}: {e.response.status_code}"
        )
        if e.response.text:
            logger.error(f"Response: {e.response.text[:500]}")
        return None
    except Exception as e:
        logger.error(f"Error fetching STAC vector features ({feature_type}): {e}")
        return None


# Convenience wrappers for specific feature types
async def fetch_lantmateriet_roads(
    bbox_wgs84: tuple[float, float, float, float],
) -> Optional[dict]:
    """Fetch road features from Lantmäteriet STAC Vektor."""
    return await fetch_stac_vector_features(bbox_wgs84, "roads")


async def fetch_lantmateriet_water(
    bbox_wgs84: tuple[float, float, float, float],
) -> Optional[dict]:
    """Fetch water/hydrography features from Lantmäteriet STAC Vektor."""
    return await fetch_stac_vector_features(bbox_wgs84, "water")


async def fetch_lantmateriet_buildings(
    bbox_wgs84: tuple[float, float, float, float],
) -> Optional[dict]:
    """Fetch building features from Lantmäteriet STAC Vektor."""
    return await fetch_stac_vector_features(bbox_wgs84, "buildings")
