"""
Lantmäteriet API integration services.

Provides access to Swedish national geodata APIs:
- STAC Höjd: High-resolution elevation (1-2 m LiDAR)
- OGC Features Hydrografi: Water bodies, waterways, wetlands
- OGC Features Marktäcke: Land cover classification (forests, urban, farmland, etc.)
- Historical Orthophotos: Aerial imagery via WMS (most recent color: 2005)
- Topowebb: Topographic map tiles (WMTS, CC-BY open)

For Swedish maps, Hydrografi and Marktäcke are used as primary data sources
for water and land cover, with OSM Overpass as automatic fallback.
Roads and individual building footprints always come from OSM.

Authentication: Basic Auth (username/password) for all APIs except Topowebb.
"""

from services.lantmateriet.auth import (
    get_basic_auth_header,
    get_authenticated_headers,
)

__all__ = [
    "get_basic_auth_header",
    "get_authenticated_headers",
]
