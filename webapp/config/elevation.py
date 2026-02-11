"""Elevation API configurations and API keys."""

import os
from dataclasses import dataclass, field


@dataclass
class CountryElevationConfig:
    """Configuration for a country's elevation data WCS/WMS API."""
    name: str
    api_type: str          # "wcs", "stac", "overpass_fallback"
    endpoint: str
    version: str = ""      # WCS version: "1.0.0", "1.1.1", "2.0.1"
    coverage_id: str = ""
    native_crs: str = ""
    resolution_m: float = 1.0
    auth_type: str = "none"  # "none", "token", "api_key", "basic"
    auth_env_var: str = ""
    supports_scalesize: bool = True  # Whether WCS 2.0.1 server supports SCALESIZE parameter
    max_request_size: int = 8192  # Maximum raster width/height the API can handle
    max_area_m: int = 0  # Maximum SUBSET area per axis in metres (0 = no limit). Requests exceeding this are chunked.
    format: str = "image/tiff"  # WCS FORMAT parameter (e.g., "image/tiff", "GeoTIFF", "GTiff")
    extra_params: dict = field(default_factory=dict)


ELEVATION_CONFIGS: dict[str, CountryElevationConfig] = {
    "NO": CountryElevationConfig(
        name="Norway",
        api_type="wcs",
        endpoint="https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833",
        version="1.0.0",
        coverage_id="NHM_DTM_25833",
        native_crs="EPSG:25833",
        resolution_m=1.0,
        auth_type="none",
        max_request_size=4096,  # Norwegian API has lower size limits
        format="GeoTIFF",  # Norwegian API requires "GeoTIFF" instead of "image/tiff"
    ),
    "EE": CountryElevationConfig(
        name="Estonia",
        api_type="wcs",
        endpoint="https://teenus.maaamet.ee/ows/wcs-dtm",
        version="2.0.1",
        coverage_id="dtm-1",
        native_crs="EPSG:3301",
        resolution_m=1.0,
        auth_type="none",
        max_request_size=4096,  # Estonian API has lower size limits than others
    ),
    "FI": CountryElevationConfig(
        name="Finland",
        api_type="wcs",
        endpoint="https://avoin-karttakuva.maanmittauslaitos.fi/ortokuvat-ja-korkeusmallit/wcs/v2",
        version="2.0.1",
        coverage_id="korkeusmalli_2m",
        native_crs="EPSG:3067",
        resolution_m=2.0,
        auth_type="api_key",
        auth_env_var="NLS_FINLAND_API_KEY",
        supports_scalesize=True,  # Verified: NLS docs confirm SCALESIZE, SCALEFACTOR, SCALEAXIS all supported
        max_request_size=5000,  # NLS docs: elevation model max 5000 px width/height
        max_area_m=10000,  # NLS docs: elevation model max 10000 x 10000 m per request
    ),
    "DK": CountryElevationConfig(
        name="Denmark",
        api_type="wcs",
        endpoint="https://api.dataforsyningen.dk/dhm_wcs_DAF",
        version="1.0.0",
        coverage_id="dhm_terraen",
        native_crs="EPSG:25832",
        resolution_m=0.4,
        auth_type="token",
        auth_env_var="DATAFORSYNINGEN_TOKEN",
        format="GTiff",  # Verified from DescribeCoverage: only supported format is "GTiff" (not "image/tiff")
        max_request_size=8192,  # Conservative limit for 0.4m data; 8192px = ~3.2km per axis
    ),
    "SE": CountryElevationConfig(
        name="Sweden",
        api_type="stac",
        endpoint="https://api.lantmateriet.se/stac-hojd/v1/",
        native_crs="EPSG:3006",
        resolution_m=1.0,
        auth_type="basic",
        auth_env_var="LANTMATERIET_USERNAME",
        extra_params={
            "password_env_var": "LANTMATERIET_PASSWORD",
        },
    ),
    "PL": CountryElevationConfig(
        name="Poland",
        api_type="wcs",
        endpoint="https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF",
        version="2.0.1",
        coverage_id="DTM_PL-KRON86-NH_TIFF",  # Verified from GetCapabilities
        native_crs="EPSG:2180",  # PUWG 1992 (Polish national grid)
        resolution_m=1.0,
        auth_type="none",  # No API key or registration required - open data (verified: Fees=none, AccessConstraints=none)
        max_request_size=4096,  # Conservative estimate, similar to Estonia
        format="image/tiff",
        supports_scalesize=True,  # Verified: service declares WCS scaling extension support
        max_area_m=5000,  # Polish geoportal silently truncates responses for large areas; observed limit ~5.5 km
    ),
    # Latvia: No WCS config - lvmgeoserver.lvm.lv only provides WMS (not WCS) for elevation.
    # The system will automatically fall back to OpenTopography Copernicus DEM 30m.
    # Lithuania: No WCS config - geoportal.lt only provides WMS (not WCS) for elevation.
    # The system will automatically fall back to OpenTopography Copernicus DEM 30m.
}

# EU-DEM fallback for countries without direct WCS raster access
EU_DEM_CONFIG = CountryElevationConfig(
    name="EU-DEM (Copernicus fallback)",
    api_type="wcs",
    endpoint="https://copernicus-dem-30m.s3.amazonaws.com",
    version="",
    native_crs="EPSG:4326",
    resolution_m=25.0,
    auth_type="none",
)

# API keys (from environment)
OPENTOPOGRAPHY_API_KEY = os.getenv("OPENTOPOGRAPHY_API_KEY", "")
DATAFORSYNINGEN_TOKEN = os.getenv("DATAFORSYNINGEN_TOKEN", "")
NLS_FINLAND_API_KEY = os.getenv("NLS_FINLAND_API_KEY", "")
# Note: Lantmäteriet uses basic auth (username/password) — see config/lantmateriet.py
