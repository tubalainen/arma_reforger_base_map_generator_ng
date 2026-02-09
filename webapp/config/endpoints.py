"""External API endpoint URLs."""

# Overpass API endpoint pool (tried in order on 429/504 failures)
# All instances serve identical OSM data — differences are only in capacity/uptime.
# Order: highest capacity/reliability first, most-overloaded last.
OVERPASS_ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",  # VK Maps — 2×56 cores, 384GB RAM, no rate limits
    "https://overpass.private.coffee/api/interpreter",           # Private.coffee — 4 servers, no rate limits
    "https://overpass.kumi.systems/api/interpreter",             # Kumi Systems — well-known mirror
    "https://overpass-api.de/api/interpreter",                   # Main instance — most overloaded, frequent 504s
]
OVERPASS_TIMEOUT = 180  # seconds

# Legacy aliases for backward compatibility
OVERPASS_ENDPOINT = OVERPASS_ENDPOINTS[0]
OVERPASS_FALLBACK_ENDPOINT = OVERPASS_ENDPOINTS[1]

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
