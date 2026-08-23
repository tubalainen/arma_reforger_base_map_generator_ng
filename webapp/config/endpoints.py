"""External API endpoint URLs."""

import os

# ---------------------------------------------------------------------------
# Overpass API pool
# ---------------------------------------------------------------------------
# Planet-wide instances. All serve identical OSM data — differences are only in
# capacity and uptime. At runtime osm_service probes every mirror and queries
# the fastest healthy one first; this list order is only the fallback used when
# the probe itself fails.
#
# `slots` is the per-IP concurrent query budget. overpass-api.de advertises
# "Rate limit: 2" on /api/status and answers a 3rd simultaneous query with a
# 504 — issue #168 was largely us exceeding this ourselves. Instances that
# advertise "Rate limit: 0" (no limit) still get a modest cap so a single job
# can't monopolise a volunteer-run server.
OVERPASS_PLANET_ENDPOINTS = [
    {
        "url": "https://overpass-api.de/api/interpreter",
        "label": "overpass-api.de",
        "slots": 2,
    },
    {
        "url": "https://overpass.private.coffee/api/interpreter",
        "label": "Private.coffee",
        "slots": 4,
    },
    {
        # VK Maps — global planet mirror, wiki states no request limits.
        # Russian-operated; set OVERPASS_DISABLE_VK=1 to drop it from the pool.
        "url": "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
        "label": "VK Maps",
        "slots": 4,
    },
]

# Regional instances. These hold a country extract, NOT the planet, so they are
# only queried when the requested bbox falls inside their coverage. Asking a
# regional mirror about anywhere else returns 200 OK with zero elements, which
# is indistinguishable from "this area genuinely has no roads" — hence the
# country gate rather than a place in the general pool.
#
# Keyed by ISO 3166-1 alpha-2 country code (matches services.country_detector).
OVERPASS_REGIONAL_ENDPOINTS = {
    "CH": [
        {
            "url": "https://overpass.osm.ch/api/interpreter",
            "label": "osm.ch",
            "slots": 4,
            "regional": True,
            # Runs Overpass 0.7.59.1, which reports `timestamp_osm_base` as a
            # bare replication sequence number ("116600") rather than an ISO
            # date. That is not corruption — see _accept_overpass_payload.
            "non_iso_timestamp": True,
        },
    ],
}

# Note: overpass.kumi.systems was removed from the pool in v1.9.0. It is a DNS
# CNAME to overpass.private.coffee (both resolve to flanders.servers.private.coffee)
# and its /api/status announces "overpass.private.coffee". Keeping both meant
# burning two full HTTP timeouts against the same dead host — see issue #168.

OVERPASS_TIMEOUT = 60          # server-side query budget: [timeout:60] in Overpass QL
OVERPASS_HTTP_TIMEOUT = 75     # httpx client timeout — server budget + 15s network buffer
OVERPASS_RETRY_HTTP_TIMEOUT = 30   # tighter budget once a mirror has already failed once
OVERPASS_PROBE_TIMEOUT = 12    # pre-flight mirror health probe — trivial query, short budget

# Merged-query budget. A single query covering all five feature categories
# returns more data than any one of them, so it gets a longer server budget.
OVERPASS_MERGED_TIMEOUT = 120
OVERPASS_MERGED_HTTP_TIMEOUT = 140


def overpass_status_url(interpreter_url: str) -> str:
    """Derive an instance's /api/status URL from its /api/interpreter URL.

    Used to read the advertised slot budget. Note that /api/status is served by
    the front-end proxy and stays 200 OK even when the Overpass backend behind
    it is returning 500s, so it reports capacity but is NOT a liveness signal.
    """
    return interpreter_url.rsplit("/", 1)[0] + "/status"


def overpass_planet_endpoints() -> list[dict]:
    """The planet pool, honouring operator opt-outs from the environment."""
    if os.getenv("OVERPASS_DISABLE_VK", "").strip().lower() in ("1", "true", "yes"):
        return [ep for ep in OVERPASS_PLANET_ENDPOINTS if ep["label"] != "VK Maps"]
    return list(OVERPASS_PLANET_ENDPOINTS)


# Legacy aliases for backward compatibility
OVERPASS_ENDPOINTS = [ep["url"] for ep in OVERPASS_PLANET_ENDPOINTS]
OVERPASS_ENDPOINT = OVERPASS_ENDPOINTS[0]
OVERPASS_FALLBACK_ENDPOINT = OVERPASS_ENDPOINTS[-1]

# ---------------------------------------------------------------------------
# Overpass response cache
# ---------------------------------------------------------------------------
# Regenerating the same square is common (tweaking a setting, retrying after a
# failed step). Caching the raw Overpass payload makes the repeat instant and
# puts zero load on the volunteer mirrors.
OVERPASS_CACHE_ENABLED = os.getenv("OVERPASS_CACHE_ENABLED", "1").strip().lower() not in (
    "0", "false", "no",
)
OVERPASS_CACHE_TTL_HOURS = int(os.getenv("OVERPASS_CACHE_TTL_HOURS", "24"))
OVERPASS_CACHE_MAX_MB = int(os.getenv("OVERPASS_CACHE_MAX_MB", "512"))

# OpenTopography Global DEM API
OPENTOPOGRAPHY_ENDPOINT = "https://portal.opentopography.org/API/globaldem"

# Satellite / land-cover imagery endpoints
SENTINEL2_WMS_ENDPOINT = "https://tiles.maps.eox.at/wms"
SENTINEL2_WMTS_URL = (
    "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/"
    "default/GoogleMapsCompatible/{z}/{y}/{x}.jpg"
)
CORINE_WMS = (
    "https://image.discomap.eea.europa.eu/arcgis/services/"
    "Corine/CLC2018_WM/MapServer/WmsServer"
)
TREE_COVER_REST = (
    "https://image.discomap.eea.europa.eu/arcgis/rest/services/"
    "GioLandPublic/HRL_TreeCoverDensity_2018/ImageServer"
)
