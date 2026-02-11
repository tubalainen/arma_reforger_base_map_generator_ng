"""
Generic OGC API Features client for Lantmäteriet APIs.

Handles paginated bbox queries against OGC API Features endpoints
(Hydrografi, Marktäcke) with Basic Auth and retry logic.

All Lantmäteriet OGC Features APIs share:
- HTTP Basic Auth (same credentials as STAC Höjd)
- WGS84 as default CRS (also supports EPSG:3006)
- GeoJSON response format (?f=json)
- limit/offset pagination (max 10,000 per page)
- bbox filtering

Usage:
    from services.lantmateriet.ogc_features_client import fetch_ogc_collection
    features = await fetch_ogc_collection(
        base_url="https://api.lantmateriet.se/ogc-features/v1/hydrografi",
        collection="StandingWater",
        bbox_wgs84=(11.9, 57.6, 12.1, 57.8),
    )
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from services.lantmateriet.auth import get_authenticated_headers

logger = logging.getLogger(__name__)

# API limits
MAX_PAGE_SIZE = 10_000  # Lantmäteriet OGC Features API maximum
DEFAULT_MAX_FEATURES = 50_000  # Safety cap per collection
REQUEST_TIMEOUT = 60.0  # Per-request timeout (seconds)
TOTAL_TIMEOUT = 300.0  # Total timeout per collection (seconds)
PAGE_DELAY = 0.2  # Polite delay between page requests (seconds)
MAX_RETRIES = 3  # Retries per failed page request


async def fetch_ogc_collection(
    base_url: str,
    collection: str,
    bbox_wgs84: tuple[float, float, float, float],
    max_features: int = DEFAULT_MAX_FEATURES,
    page_size: int = MAX_PAGE_SIZE,
    job=None,
) -> Optional[list[dict]]:
    """
    Fetch features from an OGC API Features collection with pagination.

    Queries the /collections/{collection}/items endpoint with bbox
    filtering and iterates through pages until all features are retrieved
    or the safety cap is reached.

    Args:
        base_url: API base URL (e.g. ".../hydrografi")
        collection: Collection ID (e.g. "StandingWater")
        bbox_wgs84: Bounding box as (west, south, east, north) in WGS84
        max_features: Maximum features to retrieve (safety cap)
        page_size: Features per page (max 10,000)
        job: Optional MapGenerationJob for progress logging

    Returns:
        List of GeoJSON feature dicts, or None on auth/total failure.
    """
    headers = get_authenticated_headers()
    if "Authorization" not in headers:
        logger.error(
            f"Cannot fetch OGC collection {collection}: no auth credentials"
        )
        return None

    page_size = min(page_size, MAX_PAGE_SIZE)
    west, south, east, north = bbox_wgs84
    items_url = f"{base_url}/collections/{collection}/items"

    all_features: list[dict] = []
    offset = 0

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        while len(all_features) < max_features:
            params = {
                "bbox": f"{west},{south},{east},{north}",
                "limit": str(page_size),
                "offset": str(offset),
                "f": "json",
            }

            response = await _request_with_retry(
                client, items_url, headers, params, collection
            )

            if response is None:
                # Total failure — return what we have (may be empty)
                if all_features:
                    logger.warning(
                        f"Partial fetch for {collection}: "
                        f"got {len(all_features)} features before failure"
                    )
                    return all_features
                return None

            data = response.json()
            features = data.get("features", [])
            number_returned = data.get("numberReturned", len(features))
            number_matched = data.get("numberMatched")

            all_features.extend(features)

            logger.debug(
                f"{collection}: page offset={offset}, "
                f"returned={number_returned}, total={len(all_features)}"
                + (f", matched={number_matched}" if number_matched else "")
            )

            # Stop conditions
            if number_returned < page_size:
                # Last page reached
                break

            if len(all_features) >= max_features:
                logger.warning(
                    f"{collection}: reached safety cap of {max_features} features"
                )
                break

            # Next page
            offset += number_returned

            # Polite delay between pages
            await asyncio.sleep(PAGE_DELAY)

    logger.info(
        f"Fetched {len(all_features)} features from {collection} "
        f"(bbox: {west:.3f},{south:.3f},{east:.3f},{north:.3f})"
    )

    return all_features


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict,
    collection: str,
) -> Optional[httpx.Response]:
    """
    Make a GET request with retry logic for transient errors.

    Retries on 429 (rate limit), 502, 503, 504 with exponential backoff.
    Returns None on auth failure (401/403) or exhausted retries.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, headers=headers, params=params)

            if resp.status_code == 200:
                return resp

            if resp.status_code in (401, 403):
                logger.error(
                    f"Auth failed for {collection}: HTTP {resp.status_code}. "
                    f"Check LANTMATERIET_USERNAME/PASSWORD credentials."
                )
                return None

            if resp.status_code in (429, 502, 503, 504):
                wait = 2.0 * (2 ** attempt)
                logger.warning(
                    f"{collection}: HTTP {resp.status_code}, "
                    f"retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})"
                )
                await asyncio.sleep(wait)
                continue

            # Other error — log and give up
            logger.error(
                f"{collection}: HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
            return None

        except httpx.TimeoutException:
            wait = 2.0 * (2 ** attempt)
            logger.warning(
                f"{collection}: request timeout, "
                f"retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})"
            )
            await asyncio.sleep(wait)
            continue

        except Exception as e:
            logger.error(f"{collection}: request failed: {e}")
            return None

    logger.error(f"{collection}: all {MAX_RETRIES} retries exhausted")
    return None
