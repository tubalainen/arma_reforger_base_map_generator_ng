# Data Sources & API Keys

Which elevation, imagery and vector sources are used per country, and which of
them need a (free) registration.

# Supported Countries

This application has been designed with the Nordics + Baltics in mind. There are country specific API´s for this countries to get a higher detail resolution. The application should work world wide with the fallback global API´s.

The application uses a **smart fallback system**: it tries the country-specific high-resolution API first, then falls back to OpenTopography's global Copernicus DEM (30m) if the country API is unavailable or requires an unconfigured API key.

| Country | Primary Source | Resolution | Auth Required | Fallback |
|---------|---------------|-----------|---------------|----------|
| Norway | Kartverket WCS (NHM-DTM) | 1 m | No | AWS COP30 (30m) |
| Estonia | Maa-amet WCS | 1 m | No | AWS COP30 (30m) |
| Finland | NLS WCS (korkeusmalli_2m) | 2 m | Free API key | AWS COP30 (30m) |
| Denmark | Dataforsyningen WCS (DHM) | 0.4 m | Free token | AWS COP30 (30m) |
| Sweden | Lantmäteriet STAC Höjd | 1 m | Free (basic auth) | AWS COP30 (30m) |
| Poland | GUGiK Geoportal WCS | 1 m | No | AWS COP30 (30m) |
| Latvia | (no national WCS yet) | — | — | AWS COP30 (30m) |
| Lithuania | (no national WCS yet) | — | — | AWS COP30 (30m) |
| **All other areas** | AWS COP30 (Copernicus DEM Open Data) | 30 m | **None — direct S3 read** | OpenTopography → SRTM → ALOS |

> **No API key needed for worldwide elevation.** As of v1.0.3, COP30 30 m is read directly from the AWS Open Data bucket (`copernicus-dem-30m`) — no `OPENTOPOGRAPHY_API_KEY` registration required. The OpenTopography path is kept as a same-data backup if AWS is unavailable.

> **Note:** Some country APIs have per-request area limits (e.g. Finland NLS limits elevation queries to 10 × 10 km). The application automatically splits large areas into tiles and merges the results — no user action required.

> **Sweden enhanced data:** With Lantmäteriet credentials, Swedish maps use the STAC Bild API to fetch near-current aerial orthophotos (2007–2025, 0.16 m/px) instead of Sentinel-2's 2021 imagery. Tiles are Cloud-Optimised GeoTIFFs streamed via HTTP range requests, so only the pixels needed for your area are downloaded. If STAC Bild is unavailable, the application falls back to the legacy WMS 2005 colour layer, then Sentinel-2. Map features (roads, water, buildings) always come from OpenStreetMap. If Lantmäteriet credentials are not configured, the application falls back to Sentinel-2 for imagery and OpenTopography for elevation.

# API Keys

## Worldwide elevation needs no API key

Global 30 m elevation (Copernicus DEM) is read directly from the AWS Open
Data bucket `copernicus-dem-30m` — anonymous reads, no rate limit, no
registration. This is the default for every country except the six with
high-resolution national APIs below.

## Optional backup: OpenTopography

Used only if AWS Open Data is unavailable. Same Copernicus DEM 30m data
served from a different host. Also exposes SRTM 30 m (<60°N) and
ALOS World 3D 30 m as additional fallbacks.

**Registration:** [portal.opentopography.org](https://portal.opentopography.org/) (free)
**Env Variable:** `OPENTOPOGRAPHY_API_KEY`

## Optional: Country-Specific High-Resolution Sources

Norway, Estonia, and Poland require **no API keys** — full 1 m elevation
data is freely available through open data policies.

For other countries, register for free API keys to access high-resolution
elevation data:

| Country | Registration URL | Env Variable |
|---------|-----------------|-------------|
| Finland | [maanmittauslaitos.fi](https://www.maanmittauslaitos.fi/en/rajapinnat/api-avaimen-ohje) | `NLS_FINLAND_API_KEY` |
| Denmark | [dataforsyningen.dk](https://dataforsyningen.dk/) | `DATAFORSYNINGEN_TOKEN` |
| Sweden | [apimanager.lantmateriet.se](https://apimanager.lantmateriet.se/) | `LANTMATERIET_USERNAME` + `LANTMATERIET_PASSWORD` |

> **Sweden bonus**: Lantmäteriet credentials also unlock orthophotos
> (STAC Bild, 0.16 m/px, 2007–2025) and OGC API Features for vector
> water and landcover (Hydrografi, Marktäcke).

---

[← Back to the README](../README.md)
