"""
OpenStreetMap data extraction service via Overpass API.

Extracts roads, water bodies, forests, buildings, and land use
features from OpenStreetMap using the Overpass API.

Uses a pool of public Overpass mirrors for resilience. Planet mirrors all
serve identical OSM data; regional mirrors hold only one country's extract
and are therefore gated on the detected country (asking a Swiss-only mirror
about Sweden returns 200 OK with zero elements, which is indistinguishable
from "this area has no roads").

Before each batch of feature queries a cheap pre-flight probe ranks the
mirrors by live latency and reads their advertised slot budget, so the
fastest healthy one is tried first and we stay inside its concurrency limit
instead of 504-ing ourselves (issue #168).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx

from config import (
    OVERPASS_ENDPOINTS, OVERPASS_TIMEOUT, OVERPASS_HTTP_TIMEOUT,
    OVERPASS_PROBE_TIMEOUT, OVERPASS_RETRY_HTTP_TIMEOUT,
    OVERPASS_MERGED_TIMEOUT, OVERPASS_MERGED_HTTP_TIMEOUT,
    OVERPASS_REGIONAL_ENDPOINTS,
    overpass_planet_endpoints, overpass_status_url,
)
from services import overpass_cache
from services.utils.geo import bbox_to_overpass_str
from services.utils.geojson import (
    extract_coords_from_geometry,
    close_ring,
    extract_outer_rings_from_relation,
    make_polygon_or_multi,
)

logger = logging.getLogger(__name__)

USER_AGENT = "ArmaReforgerMapGenerator/1.0"


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


def _endpoint_label(endpoint) -> str:
    """Short human-readable label for an endpoint dict or bare URL."""
    if isinstance(endpoint, dict):
        return endpoint.get("label") or _endpoint_label(endpoint["url"])
    try:
        from urllib.parse import urlparse
        return urlparse(endpoint).hostname or endpoint
    except Exception:
        return endpoint


def _as_endpoint(endpoint) -> dict:
    """Normalise a bare URL into the endpoint dict shape used internally."""
    if isinstance(endpoint, dict):
        return endpoint
    return {"url": endpoint, "label": _endpoint_label(endpoint), "slots": 2}


# ---------------------------------------------------------------------------
# Feature selectors
# ---------------------------------------------------------------------------
# Defined once and shared by the merged query, the per-category fallback
# queries, and the client-side classifier. Keeping a single source of truth is
# what makes "one merged query split client-side" provably equivalent to five
# separate queries — the classifier below mirrors these selectors exactly.

_ROAD_VALUES = (
    "motorway|motorway_link|trunk|trunk_link|primary|primary_link|"
    "secondary|secondary_link|tertiary|tertiary_link|residential|"
    "unclassified|service|track|path|footway|cycleway|bridleway|living_street"
)
_WATERWAY_VALUES = "river|stream|canal|ditch|drain"
_LANDUSE_VALUES = (
    "farmland|meadow|orchard|vineyard|residential|industrial|commercial|"
    "retail|quarry|cemetery|allotments|recreation_ground|military|farmyard"
)
_LEISURE_VALUES = "park|garden|pitch|playground|golf_course"
_NATURAL_LANDUSE_VALUES = "beach|sand|bare_rock|scree|grassland|fell"

CATEGORY_SELECTORS: dict[str, list[str]] = {
    "roads": [
        f'way["highway"~"^({_ROAD_VALUES})$"];',
    ],
    "water": [
        'way["natural"="water"];',
        'relation["natural"="water"];',
        f'way["waterway"~"^({_WATERWAY_VALUES})$"];',
        'way["natural"="coastline"];',
        'way["natural"="wetland"];',
        'relation["natural"="wetland"];',
    ],
    "forests": [
        'way["natural"="wood"];',
        'relation["natural"="wood"];',
        'way["landuse"="forest"];',
        'relation["landuse"="forest"];',
        'way["natural"="scrub"];',
        'way["natural"="heath"];',
        'way["natural"="tree_row"];',
    ],
    "buildings": [
        'way["building"];',
        'relation["building"];',
    ],
    "land_use": [
        f'way["landuse"~"^({_LANDUSE_VALUES})$"];',
        f'relation["landuse"~"^({_LANDUSE_VALUES})$"];',
        f'way["leisure"~"^({_LEISURE_VALUES})$"];',
        f'way["natural"~"^({_NATURAL_LANDUSE_VALUES})$"];',
    ],
}

ALL_CATEGORIES = tuple(CATEGORY_SELECTORS)

_ROAD_SET = set(_ROAD_VALUES.split("|"))
_WATERWAY_SET = set(_WATERWAY_VALUES.split("|"))
_LANDUSE_SET = set(_LANDUSE_VALUES.split("|"))
_LEISURE_SET = set(_LEISURE_VALUES.split("|"))
_NATURAL_LANDUSE_SET = set(_NATURAL_LANDUSE_VALUES.split("|"))


def _matches_category(category: str, elem_type: str, tags: dict) -> bool:
    """Does this element match `category`'s Overpass selectors?

    One element can match several categories (a `building=yes` way that is also
    `landuse=retail`, say). The five separate queries returned it in both
    result sets, so the classifier must too — anything else would silently
    change the feature counts and the downstream surface masks.
    """
    natural = tags.get("natural", "")
    landuse = tags.get("landuse", "")

    if category == "roads":
        return elem_type == "way" and tags.get("highway", "") in _ROAD_SET

    if category == "water":
        if elem_type == "way":
            return (
                natural in ("water", "coastline", "wetland")
                or tags.get("waterway", "") in _WATERWAY_SET
            )
        if elem_type == "relation":
            return natural in ("water", "wetland")
        return False

    if category == "forests":
        if elem_type in ("way", "relation") and (natural == "wood" or landuse == "forest"):
            return True
        return elem_type == "way" and natural in ("scrub", "heath", "tree_row")

    if category == "buildings":
        return elem_type in ("way", "relation") and "building" in tags

    if category == "land_use":
        if elem_type == "relation":
            return landuse in _LANDUSE_SET
        if elem_type == "way":
            return (
                landuse in _LANDUSE_SET
                or tags.get("leisure", "") in _LEISURE_SET
                or natural in _NATURAL_LANDUSE_SET
            )
        return False

    return False


def split_elements_by_category(
    elements: list, categories=ALL_CATEGORIES
) -> dict[str, list]:
    """Split a merged Overpass response back into per-category element lists."""
    buckets: dict[str, list] = {c: [] for c in categories}
    for elem in elements:
        elem_type = elem.get("type", "")
        tags = elem.get("tags", {}) or {}
        for category in categories:
            if _matches_category(category, elem_type, tags):
                buckets[category].append(elem)
    return buckets


def build_overpass_query(bbox: dict, categories=ALL_CATEGORIES, timeout: int = None) -> str:
    """Build a single Overpass query covering every requested category."""
    selectors = []
    for category in categories:
        selectors.extend(CATEGORY_SELECTORS[category])
    body = "\n      ".join(selectors)
    budget = timeout if timeout is not None else OVERPASS_TIMEOUT
    return f"""
    [out:json][timeout:{budget}][bbox:{_bbox_to_overpass(bbox)}];
    (
      {body}
    );
    out body geom;
    """


def _is_valid_iso_timestamp(value: str) -> bool:
    """Check that an Overpass `timestamp_osm_base` string parses as an ISO date.

    Most mirrors return values like "2026-05-06T03:25:00Z". Older builds
    (Overpass 0.7.59.1, as run by overpass.osm.ch) instead report a bare
    replication sequence number like "116600". That is a metadata quirk, not
    corruption — see `_accept_overpass_payload` for how the two are told apart.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        # fromisoformat doesn't accept the trailing "Z" before Python 3.11,
        # so strip it. Anything that parses is good enough.
        datetime.fromisoformat(value.rstrip("Z"))
        return True
    except ValueError:
        return False


def _accept_overpass_payload(result: dict, endpoint: dict) -> tuple[bool, str]:
    """Decide whether a 200 OK Overpass payload is actually usable.

    Returns (accepted, reason). A 200 with valid JSON is not proof of a usable
    response, so three failure modes are screened out:

    * A `remark` with zero elements — Overpass reports runtime errors (query
      timeout, memory limit) this way, at HTTP 200.
    * A non-ISO `timestamp_osm_base` with zero elements — the mirror is either
      broken or, more often, holds a regional extract that doesn't cover this
      bbox. Either way the empty result is not trustworthy as "no features".
    * A non-ISO timestamp *with* elements is accepted: data beats metadata.
      overpass.osm.ch answers Swiss bboxes correctly while always reporting
      "116600", and rejecting that would throw away a good Swiss map.
    """
    elements = result.get("elements", [])
    remark = result.get("remark", "")
    timestamp = result.get("osm3s", {}).get("timestamp_osm_base", "")

    if remark and not elements:
        return False, f"soft error: {remark!r}"

    if not _is_valid_iso_timestamp(timestamp):
        if elements:
            if not endpoint.get("non_iso_timestamp"):
                logger.info(
                    f"Overpass [{_endpoint_label(endpoint)}] reports a non-ISO "
                    f"timestamp_osm_base ({timestamp!r}) but returned "
                    f"{len(elements)} elements — accepting the data"
                )
            return True, ""
        return False, (
            f"non-ISO timestamp_osm_base ({timestamp!r}) with zero elements — "
            f"mirror is broken or does not cover this area"
        )

    return True, ""


def _rank_mirrors(probe_results: list[tuple]) -> list:
    """Order mirrors best-first from probe results.

    Healthy mirrors (probe returned a usable response) come first, sorted by
    ascending probe latency. Unhealthy mirrors keep their original pool order
    and go last — demoted, never dropped, so the caller can still fall back to
    the full list when nothing passed the probe.

    Args:
        probe_results: (endpoint, healthy, latency_seconds) tuples in pool order.
    """
    healthy = sorted(
        ((ep, latency) for ep, ok, latency in probe_results if ok),
        key=lambda item: item[1],
    )
    unhealthy = [ep for ep, ok, _ in probe_results if not ok]
    return [ep for ep, _ in healthy] + unhealthy


# Trivial query for the pre-flight health probe. Node 1 has existed in OSM
# since 2005, so any healthy planet mirror answers it in well under a second.
#
# It sits in Italy, though, so a regional mirror legitimately returns zero
# elements for it — see `_probe_verdict`.
_PROBE_QUERY = "[out:json][timeout:25];node(1);out;"


def _probe_verdict(result: dict, endpoint: dict) -> tuple[bool, str]:
    """Judge a probe response.

    Planet mirrors are held to the full payload check. Regional mirrors are
    only checked for liveness: the probe queries node(1), which lies outside
    their extract by construction, so an empty answer proves nothing either
    way. Whether such a mirror can actually serve the requested area is
    settled by `_accept_overpass_payload` on the real query, which falls
    through to the planet pool if it can't.
    """
    if endpoint.get("regional"):
        if "osm3s" in result:
            return True, ""
        return False, "response has no osm3s block"
    return _accept_overpass_payload(result, endpoint)

_SLOT_RE = re.compile(r"^Rate limit:\s*(\d+)", re.MULTILINE)


def parse_slot_budget(status_text: str, default: int) -> int:
    """Read the advertised concurrent-query budget from an /api/status body.

    Overpass reports `Rate limit: N`, where 0 means "no limit". We still cap
    unlimited instances at `default` so one job can't monopolise a
    volunteer-run server.
    """
    match = _SLOT_RE.search(status_text or "")
    if not match:
        return default
    advertised = int(match.group(1))
    if advertised == 0:
        return default
    return max(1, min(advertised, default))


async def _read_slot_budget(client: httpx.AsyncClient, endpoint: dict) -> int:
    """Fetch and parse an instance's slot budget, falling back to its config."""
    default = endpoint.get("slots", 2)
    try:
        resp = await client.get(
            overpass_status_url(endpoint["url"]),
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return default
        return parse_slot_budget(resp.text, default)
    except Exception:
        return default


async def _probe_one_mirror(
    client: httpx.AsyncClient, endpoint: dict
) -> tuple[dict, bool, float]:
    """Probe a single mirror. Returns (endpoint, healthy, latency_seconds).

    Two signals are needed. `/api/status` gives the slot budget but is served
    by the front-end proxy and stays 200 OK while the Overpass backend behind
    it returns 500s — so liveness has to come from a real interpreter query.
    """
    label = _endpoint_label(endpoint)
    start = time.monotonic()
    # Capacity and liveness are fetched together; the status read is always
    # awaited, even on failure, so it can never be left pending.
    status_task = asyncio.create_task(_read_slot_budget(client, endpoint))
    try:
        resp = await client.post(
            endpoint["url"],
            data={"data": _PROBE_QUERY},
            headers={"User-Agent": USER_AGENT},
            timeout=OVERPASS_PROBE_TIMEOUT,
        )
    except Exception as e:
        latency = time.monotonic() - start
        endpoint["slots"] = await status_task
        logger.info(
            f"Overpass probe [{label}]: {type(e).__name__} after {latency:.1f}s — skipping"
        )
        return endpoint, False, latency

    latency = time.monotonic() - start
    endpoint["slots"] = await status_task

    if resp.status_code != 200:
        logger.info(
            f"Overpass probe [{label}]: HTTP {resp.status_code} in {latency:.1f}s — skipping"
        )
        return endpoint, False, latency

    try:
        payload = resp.json()
    except Exception:
        logger.info(f"Overpass probe [{label}]: unparseable JSON in {latency:.1f}s — skipping")
        return endpoint, False, latency

    accepted, reason = _probe_verdict(payload, endpoint)
    if accepted:
        logger.info(
            f"Overpass probe [{label}]: healthy in {latency:.1f}s "
            f"({endpoint['slots']} slot(s))"
        )
        return endpoint, True, latency

    logger.info(f"Overpass probe [{label}]: {reason} in {latency:.1f}s — skipping")
    return endpoint, False, latency


def _pool_for_country(country: Optional[str]) -> list[dict]:
    """Planet mirrors, plus any regional mirror that covers `country`.

    Regional mirrors are only ever offered for their own country. They hold a
    single-country extract, so outside it they answer every query with zero
    elements — a silent wrong answer rather than a visible failure.
    """
    pool = [dict(ep) for ep in overpass_planet_endpoints()]
    for ep in OVERPASS_REGIONAL_ENDPOINTS.get((country or "").upper(), []):
        pool.append(dict(ep))
    return pool


async def probe_overpass_mirrors(job=None, country: Optional[str] = None) -> list[dict]:
    """Probe every candidate mirror in parallel; return the healthy ones, fastest first.

    The fastest healthy Overpass mirror changes hour to hour, so rather than
    relying on a fixed priority list this measures it: one trivial query to
    every mirror at once, ranked by latency.

    Mirrors that fail the probe are **dropped**, not merely demoted. Issue #168
    showed the cost of keeping them: the probe correctly flagged three dead
    mirrors, then the query loop spent 2m35s walking them anyway before
    succeeding on the one the probe had already picked. Only when *every*
    mirror fails does the full pool come back, on the theory that a probe-wide
    failure says more about our network than about the mirrors.
    """
    pool = _pool_for_country(country)
    async with httpx.AsyncClient(timeout=OVERPASS_PROBE_TIMEOUT) as client:
        results = await asyncio.gather(
            *(_probe_one_mirror(client, ep) for ep in pool)
        )

    healthy = [ep for ep, ok, _ in results if ok]

    if not healthy:
        logger.warning(
            "Overpass probe: every mirror failed — falling back to the full pool"
        )
        if job:
            job.add_log(
                "All Overpass mirrors failed the health probe — trying them all anyway",
                "warning",
            )
        return _rank_mirrors(list(results))

    ordered = [ep for ep in _rank_mirrors(list(results)) if ep in healthy]
    logger.info(
        f"Overpass probe: {len(healthy)}/{len(pool)} mirrors healthy — "
        f"query order: {[_endpoint_label(ep) for ep in ordered]}"
    )
    if job:
        job.add_log(
            f"Overpass: {len(healthy)} of {len(pool)} mirror(s) healthy — "
            f"using [{_endpoint_label(ordered[0])}] first"
        )
    return ordered


# Per-endpoint concurrency gates, keyed by interpreter URL. Overpass instances
# publish a per-IP slot budget (overpass-api.de advertises 2); exceeding it
# earns a 504, which is exactly what issue #168 reported. One semaphore per
# host keeps every query in a job — and across concurrent jobs — inside it.
_endpoint_gates: dict[str, tuple[asyncio.Semaphore, int]] = {}


def _gate_for(endpoint: dict) -> asyncio.Semaphore:
    slots = max(1, int(endpoint.get("slots", 2)))
    gate, known_slots = _endpoint_gates.get(endpoint["url"], (None, None))
    if gate is None or known_slots != slots:
        gate = asyncio.Semaphore(slots)
        _endpoint_gates[endpoint["url"]] = (gate, slots)
    return gate


def describe_categories(categories) -> str:
    """Human-readable label for a set of categories, used in log lines."""
    names = [c.replace("_", " ") for c in categories]
    if len(names) >= len(ALL_CATEGORIES):
        return "all features"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


async def _try_endpoint(
    client: httpx.AsyncClient,
    endpoint: dict,
    query: str,
    query_type: str,
    http_timeout: float,
    job=None,
) -> tuple[Optional[dict], bool]:
    """Run `query` against one mirror.

    Returns (payload, retryable). `retryable` is False when the mirror failed
    in a way that won't change on a second pass within this job (a hard 5xx,
    a timeout, a broken payload), so the caller can strike it off instead of
    paying its timeout again — the 2m35s of waste in issue #168.
    """
    label = _endpoint_label(endpoint)
    try:
        async with _gate_for(endpoint):
            resp = await client.post(
                endpoint["url"],
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=http_timeout,
            )
    except Exception as e:
        # httpx timeout exceptions stringify to "", so log the class name —
        # issue #168's log had a bare "request failed:" with no cause.
        logger.warning(f"Overpass [{label}] request failed: {type(e).__name__}")
        return None, False

    if resp.status_code == 200:
        content_type = resp.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            logger.warning(
                f"Overpass [{label}] returned non-JSON response "
                f"(Content-Type: {content_type}), trying next..."
            )
            logger.debug(f"Response preview: {resp.text[:500]}")
            return None, False

        try:
            result = resp.json()
        except json.JSONDecodeError as json_err:
            logger.error(f"Overpass [{label}] returned invalid JSON: {json_err}")
            logger.debug(f"Response preview: {resp.text[:500]}")
            return None, False

        accepted, reason = _accept_overpass_payload(result, endpoint)
        if not accepted:
            logger.warning(f"Overpass [{label}] rejected: {reason} — trying next...")
            if job:
                job.add_log(f"Overpass mirror [{label}] returned unusable data, trying next...", "warning")
            # A regional mirror that doesn't cover this bbox will never cover
            # it; a soft error might clear, but not soon enough to be worth
            # another full timeout inside this job.
            return None, False

        logger.info(
            f"Successfully fetched {query_type} from Overpass [{label}]: "
            f"{len(result.get('elements', []))} elements, {len(resp.content) / 1024:.1f} KB"
        )
        return result, False

    if resp.status_code == 429:
        logger.warning(f"Overpass [{label}] rate limited (429), trying next...")
        if job:
            job.add_log(f"Overpass mirror [{label}] rate limited, trying next...", "warning")
        return None, True  # a slot may free up

    if resp.status_code == 504:
        logger.warning(f"Overpass [{label}] timeout (504), trying next...")
        if job:
            job.add_log(f"Overpass mirror [{label}] timed out, trying next...", "warning")
        return None, True  # load-shedding; often succeeds on a later pass

    if resp.status_code in (502, 503):
        logger.warning(f"Overpass [{label}] unavailable ({resp.status_code}), trying next...")
        if job:
            job.add_log(f"Overpass mirror [{label}] unavailable ({resp.status_code}), trying next...", "warning")
        return None, False

    logger.error(f"Overpass [{label}] error {resp.status_code}: {resp.text[:300]}")
    return None, False


async def _run_overpass_query(
    query: str,
    max_retries: int = 2,
    job=None,
    endpoints: Optional[list] = None,
    http_timeout: float = OVERPASS_HTTP_TIMEOUT,
    query_type: str = "features",
) -> Optional[dict]:
    """
    Execute an Overpass API query against a pool of mirrors.

    Answers from the on-disk cache when the identical query was run recently.
    Otherwise walks the (probe-ordered) mirror list, moving on when a mirror
    rate-limits, times out, or returns an unusable payload. Mirrors that fail
    in a way a retry can't fix are struck off for the rest of this call, so a
    dead host costs one timeout rather than one per pass.

    Args:
        query: Overpass QL query string
        max_retries: Number of full passes through the endpoint pool
        endpoints: Ordered endpoint list (e.g. from probe_overpass_mirrors);
            defaults to the configured pool order when omitted.
        http_timeout: httpx client timeout for each attempt.
        query_type: what the query is fetching, for log lines.
    """
    cached = overpass_cache.load(query)
    if cached is not None:
        if job:
            job.add_log("Reusing cached OpenStreetMap data for this area")
        return cached

    pool = [_as_endpoint(ep) for ep in (endpoints or OVERPASS_ENDPOINTS)]
    dead: set[str] = set()

    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            # Later passes use a tighter budget: a mirror that needed more than
            # 30s on the first pass is not the one that will rescue this query.
            timeout = http_timeout if attempt == 0 else min(http_timeout, OVERPASS_RETRY_HTTP_TIMEOUT)
            candidates = [ep for ep in pool if ep["url"] not in dead]

            if not candidates:
                logger.warning(
                    f"Overpass: every mirror is struck off for {query_type} — nothing left to try"
                )
                break

            for endpoint in candidates:
                result, retryable = await _try_endpoint(
                    client, endpoint, query, query_type, timeout, job
                )
                if result is not None:
                    overpass_cache.store(query, result)
                    return result
                if not retryable:
                    dead.add(endpoint["url"])

            if attempt < max_retries - 1:
                if len(dead) >= len(pool):
                    # Striking mirrors off is only worth it while better ones
                    # remain. Once the whole pool is struck off there is nothing
                    # cheaper to try, so give it one more pass after the backoff
                    # rather than failing a query a retry might have rescued.
                    logger.info(
                        "Overpass: whole pool struck off — clearing strikes for one more pass"
                    )
                    dead.clear()
                remaining = [ep for ep in pool if ep["url"] not in dead]
                wait = 10 * (2 ** attempt)  # exponential: 10s, 20s, ...
                logger.warning(
                    f"All {len(candidates)} Overpass endpoint(s) failed for {query_type}, "
                    f"retrying {len(remaining)} in {wait}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait)

    logger.error("All Overpass endpoints failed after all retries - continuing with partial data")
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


# ---------------------------------------------------------------------------
# Category plumbing
# ---------------------------------------------------------------------------
# Each category pairs its Overpass selectors (CATEGORY_SELECTORS, above) with
# an element processor and the wording of its log lines. Going through one
# table is what lets the merged query and the per-category fallback produce
# byte-identical output.

def _processor(category: str):
    return {
        "roads": lambda els: _process_road_elements(els),
        "water": lambda els: _process_water_elements(els),
        "forests": lambda els: _process_area_elements(els, "forest"),
        "buildings": lambda els: _process_building_elements(els),
        "land_use": lambda els: _process_area_elements(els, "land_use"),
    }[category]


# category -> (property counted, singular log noun, job-log label, job-log noun)
_CATEGORY_LOG = {
    "roads": ("highway", "road segments", "Roads", "segments"),
    "water": ("water_type", "water features", "Water", "features"),
    "forests": ("type", "forest/woodland features", "Forests", "areas"),
    "buildings": ("building_type", "building footprints", "Buildings", "structures"),
    "land_use": ("type", "land use features", "Land use", "areas"),
}


def _build_feature_collection(category: str, elements: list, job=None) -> dict:
    """Process raw Overpass elements into a GeoJSON FeatureCollection and log it."""
    features = _processor(category)(elements)
    prop, noun, job_label, job_noun = _CATEGORY_LOG[category]

    counts: dict[str, int] = {}
    for feat in features:
        value = feat["properties"].get(prop, "unknown")
        counts[value] = counts.get(value, 0) + 1

    top_types = dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5])
    if category == "roads":
        logger.info(
            f"Fetched {len(features)} {noun} across {len(counts)} types: {top_types}"
        )
    elif category in ("water", "forests"):
        logger.info(f"Fetched {len(features)} {noun}: {counts}")
    else:
        logger.info(f"Fetched {len(features)} {noun}. Top types: {top_types}")

    if job:
        source = counts if category in ("water", "forests") else top_types
        details = ", ".join(f"{k}: {v}" for k, v in list(source.items())[:5])
        job.add_log(f"✓ {job_label}: {len(features)} {job_noun} ({details})", "success")

    return {"type": "FeatureCollection", "features": features}


async def _fetch_category(
    bbox: dict, category: str, job=None, endpoints=None
) -> Optional[dict]:
    """Fetch a single feature category with its own Overpass query."""
    query = build_overpass_query(bbox, [category])
    logger.info(
        f"Fetching {category.replace('_', ' ')} from Overpass API "
        f"(bbox: {_bbox_to_overpass(bbox)})..."
    )
    result = await _run_overpass_query(
        query, job=job, endpoints=endpoints, query_type=describe_categories([category])
    )

    if result and "elements" in result:
        return _build_feature_collection(category, result["elements"], job)

    logger.warning(f"No {category.replace('_', ' ')} data returned from Overpass API")
    return None


async def fetch_roads(bbox: dict, job=None, endpoints=None) -> Optional[dict]:
    """Fetch the road network from OSM (classification, surface, width, ...)."""
    return await _fetch_category(bbox, "roads", job, endpoints)


async def fetch_water(bbox: dict, job=None, endpoints=None) -> Optional[dict]:
    """Fetch water features from OSM (lakes, rivers, coastline, wetlands)."""
    return await _fetch_category(bbox, "water", job, endpoints)


async def fetch_forests(bbox: dict, job=None, endpoints=None) -> Optional[dict]:
    """Fetch forest and woodland areas from OSM (forest, wood, scrub, tree rows)."""
    return await _fetch_category(bbox, "forests", job, endpoints)


async def fetch_buildings(bbox: dict, job=None, endpoints=None) -> Optional[dict]:
    """Fetch building footprints from OSM (type, height, levels)."""
    return await _fetch_category(bbox, "buildings", job, endpoints)


async def fetch_land_use(bbox: dict, job=None, endpoints=None) -> Optional[dict]:
    """Fetch land use areas from OSM (farmland, meadow, residential, ...)."""
    return await _fetch_category(bbox, "land_use", job, endpoints)


def _empty_fc() -> dict:
    return {"type": "FeatureCollection", "features": []}


async def fetch_categories(
    bbox: dict,
    categories=ALL_CATEGORIES,
    job=None,
    endpoints=None,
) -> dict[str, dict]:
    """Fetch several feature categories in ONE Overpass query, split client-side.

    Five separate queries need five query slots. overpass-api.de advertises two
    per IP and answers the overflow with a 504 — issue #168's roads failure was
    us competing with our own buildings query. A single unioned query needs one
    slot no matter how many categories it covers.

    Falls back to per-category queries if the merged query fails, since a large
    square asking for everything can legitimately exceed a mirror's memory or
    time budget where the individual queries would each fit.
    """
    categories = list(categories)
    if not categories:
        return {}

    query = build_overpass_query(bbox, categories, timeout=OVERPASS_MERGED_TIMEOUT)
    logger.info(
        f"Fetching {', '.join(categories)} from Overpass API in one query "
        f"(bbox: {_bbox_to_overpass(bbox)})..."
    )
    if job:
        job.add_log(
            f"Fetching {len(categories)} feature categories from OpenStreetMap "
            f"in a single query..."
        )

    result = await _run_overpass_query(
        query,
        job=job,
        endpoints=endpoints,
        http_timeout=OVERPASS_MERGED_HTTP_TIMEOUT,
        query_type=describe_categories(categories),
    )

    if result and "elements" in result:
        buckets = split_elements_by_category(result["elements"], categories)
        return {
            category: _build_feature_collection(category, elements, job)
            for category, elements in buckets.items()
        }

    logger.warning(
        "Merged Overpass query failed — falling back to one query per category"
    )
    if job:
        job.add_log(
            "Combined OpenStreetMap query failed, retrying one category at a time...",
            "warning",
        )

    # Serial, not concurrent: the merged query already failed, so the pool is
    # under strain and piling five parallel queries onto it makes things worse.
    results: dict[str, dict] = {}
    for category in categories:
        try:
            results[category] = await _fetch_category(bbox, category, job, endpoints) or _empty_fc()
        except Exception as e:
            logger.error(f"Failed to fetch {category}: {type(e).__name__}: {e}")
            if job:
                job.add_log(f"Warning: Failed to fetch {category}: {e}", "warning")
            results[category] = _empty_fc()
    return results


async def fetch_all_features(bbox: dict, job=None, country: Optional[str] = None) -> dict:
    """
    Fetch all OSM features for a bounding box.
    Returns dict with roads, water, forests, buildings, land_use collections.

    Probes the mirror pool once, then runs a single merged query against the
    fastest healthy mirror and splits the response by category.
    """
    if job:
        job.add_log("Checking Overpass mirror health...")
        job.progress = 27
    endpoints = await probe_overpass_mirrors(job, country)

    result = await fetch_categories(bbox, ALL_CATEGORIES, job, endpoints)
    result = {category: result.get(category) or _empty_fc() for category in ALL_CATEGORIES}

    if job:
        job.progress = 35
        counts = {k: len(v.get("features", [])) for k, v in result.items()}
        job.add_log(
            f"Fetched {counts['roads']} roads, {counts['water']} water features, "
            f"{counts['forests']} forests, {counts['buildings']} buildings, "
            f"{counts['land_use']} land use areas",
            "success"
        )

    return result
