"""
Lantmäteriet API integration services.

Provides access to Swedish national geodata APIs:
- STAC Höjd: High-resolution elevation (1-2 m LiDAR)
- Historical Orthophotos: Aerial imagery via WMS (most recent color: 2005)
- Topowebb: Topographic map tiles (WMTS, CC-BY open)

Note: STAC Vektor was evaluated but provides municipality-level bulk downloads
(ZIP/GeoPackage), not feature-level queries. OSM Overpass is used for all
map features (roads, water, buildings, forests).

Authentication: Basic Auth (username/password) for STAC and WMS APIs.
Topowebb is open data (CC-BY, no auth required).
"""

from services.lantmateriet.auth import (
    get_basic_auth_header,
    get_authenticated_headers,
)

__all__ = [
    "get_basic_auth_header",
    "get_authenticated_headers",
]
