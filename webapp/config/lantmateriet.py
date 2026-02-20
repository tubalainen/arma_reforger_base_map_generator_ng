"""Lantmäteriet API configurations.

Provides configuration for Swedish Lantmäteriet APIs:
- STAC Höjd (elevation, 1-2 m LiDAR)
- STAC Bild (orthophotos, 0.16 m/px, 2007–2025)
- Historical Orthophotos WMS (aerial imagery, most recent color: 2005 — fallback)
- Topowebb WMTS (topographic map tiles, CC-BY open)

Authentication: Basic Auth (username/password) over HTTPS.
"""

import os
from dataclasses import dataclass


@dataclass
class LantmaterietConfig:
    """Lantmäteriet API endpoints and settings."""

    # Authentication
    username: str = ""
    password: str = ""

    # STAC APIs
    stac_hojd_endpoint: str = "https://api.lantmateriet.se/stac-hojd/v1/"
    stac_bild_endpoint: str = "https://api.lantmateriet.se/stac-bild/v1/"
    stac_vektor_endpoint: str = "https://api.lantmateriet.se/stac-vektor/v1"

    # WMS/WMTS Services (WMS orthophotos used as fallback for STAC Bild)
    orthophoto_wms: str = "https://maps.lantmateriet.se/historiska-ortofoton/wms/v1"
    topowebb_wmts: str = "https://maps.lantmateriet.se/open/topowebb-ccby/v1/wmts"

    # Settings
    native_crs: str = "EPSG:3006"  # SWEREF99 TM
    elevation_resolution_m: float = 1.0
    max_tile_size: int = 4096

    def has_credentials(self) -> bool:
        """Check if authentication credentials are configured."""
        return bool(self.username and self.password)


def _load_config() -> LantmaterietConfig:
    """
    Load config from environment variables.

    Uses a factory function instead of dataclass field defaults to avoid
    the issue where os.getenv() default values are evaluated once at class
    definition time — inside Docker, the environment may not be ready when
    the module is first imported by another module at startup.
    """
    return LantmaterietConfig(
        username=os.getenv("LANTMATERIET_USERNAME", ""),
        password=os.getenv("LANTMATERIET_PASSWORD", ""),
    )


# Global instance (loaded from env at import time)
LANTMATERIET_CONFIG = _load_config()
