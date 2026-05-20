"""External API endpoint URLs."""

# Overpass API endpoint pool. All instances serve identical OSM data —
# differences are only in capacity/uptime. At runtime osm_service probes
# every mirror and queries the fastest healthy one first; this list order
# is only the fallback used when the probe itself fails.
OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",           # Private.coffee — 4 servers, no rate limits
    "https://overpass.osm.ch/api/interpreter",                   # osm.ch — Swiss-hosted, full planet database
    "https://overpass.kumi.systems/api/interpreter",             # Kumi Systems — well-known mirror
    "https://overpass-api.de/api/interpreter",                   # Main instance — most overloaded, frequent 504s
]
OVERPASS_TIMEOUT = 60          # server-side query budget: [timeout:60] in Overpass QL
OVERPASS_HTTP_TIMEOUT = 75     # httpx client timeout — server budget + 15s network buffer
OVERPASS_PROBE_TIMEOUT = 12    # pre-flight mirror health probe — trivial query, short budget

# Legacy aliases for backward compatibility
OVERPASS_ENDPOINT = OVERPASS_ENDPOINTS[0]
OVERPASS_FALLBACK_ENDPOINT = OVERPASS_ENDPOINTS[3]   # overpass-api.de (shifted to index 3)

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
