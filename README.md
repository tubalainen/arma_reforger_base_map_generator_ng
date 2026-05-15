# Arma Reforger Base Map Generator

**Automate the tedious work of creating custom Arma Reforger maps.** This tool generates realistic terrain from real-world geodata, eliminating hours of manual work in the [Arma Reforger World Editor](https://community.bistudio.com/wiki/Arma_Reforger:World_Editor).

Instead of manually sourcing elevation data, painting surface masks by hand, placing roads one-by-one, and sculpting terrain around features, simply draw a square or rectangle on the interactive map and get Enfusion-ready heightmaps, surface masks, and vector data in minutes.

<img width="1920" height="1152" alt="image" src="https://github.com/user-attachments/assets/31b9a3de-3581-47aa-b060-ee56fbcf73f6" />

## Features

### Elevation Data with Intelligent Fallback
- **High-resolution national LiDAR** (0.4m-2m) from country-specific APIs
- **Sweden**: Lantmäteriet STAC Höjd (1 m LiDAR) with basic authentication
- **Worldwide 30m elevation with no API key required** — Copernicus DEM 30m streamed directly from AWS Open Data (`copernicus-dem-30m` bucket)
- **Multi-source fallback chain**: Country WCS → AWS COP30 (no auth) → OpenTopography COP30 → SRTM → ALOS
- **Automatic coastal/ocean handling** — coastal selections no longer false-positive as "DEM truncated"

### Automated Terrain Generation
- **13-step generation pipeline** with real-time progress tracking
- **Heightmap refinement**: Road flattening with Gaussian smoothing, water body leveling
- **16-bit heightmap export** in PNG and ESRI ASCII Grid (.asc) formats
- **Treeline-aware surface generation** (rock above country-specific treeline elevations)

### Surface Masks (9 Materials for Enfusion)
- **Grass/meadow** — default surface, complement of all others
- **Deciduous forest floor** and **coniferous (pine) forest floor** — separated by OSM `leaf_type`
- **Rock** — slopes >25° and above the country-specific treeline
- **Asphalt** — paved road buffers + urban areas
- **Gravel** — gravel/unpaved roads (track, path)
- **Dirt** — farmland, dirt paths
- **Sand** — shoreline transition zone (no longer paints lake interiors as "seabed")
- **Water edge** — outer ring transition around water polygons
- **Empty masks are auto-skipped** — sand/water_edge/dirt/etc. are omitted from the ZIP if they have no meaningful coverage in your area
- **GeoJSON polygon holes are honored** — islands inside lakes are correctly rendered as land in every surface mask

### Road Networks
- **Complete OSM road classification** (motorway to footpath)
- **Country-specific surface inference** when OSM data is missing
- **Enfusion prefab mapping** (RG_Road_* generators)
- **Spline control point generation** for World Editor import
- **Multi-mirror Overpass API pool** for reliable OSM data fetching (Private.coffee, osm.ch, Kumi, overpass-api.de) with exponential backoff and automatic failover

### Water Features
- **Lakes, rivers, streams, coastline, wetlands** from OSM
- **Sweden**: Lantmäteriet Hydrografi OGC API (StandingWater, WatercourseLine/Polygon, Wetland) when credentials are configured
- **Flat water surface elevation** with smooth shoreline transitions
- **Auto-emitted closed splines** in `*_water.layer` — one per lake/pond/reservoir, ready to drop a Lake Generator prefab onto
- **GeoJSON export** with structured metadata

### Building & Vegetation Data
- **Building footprints** with height estimation and rotation
- **Forest areas** with species classification (coniferous/deciduous/mixed)
- **Sweden**: Lantmäteriet Marktäcke OGC API for landcover and wetlands
- **Auto-emitted closed splines** in `*_vegetation.layer` — one per forest polygon, ready to drop a Forest Generator prefab onto
- **Structured JSON export** for object placement

### Multi-User Support & Security
- **Session management** for concurrent users
- **Job isolation** (users can only access their own generations)
- **Rate limiting** (60 requests/min, 10 generations/hour)
- **Real-time job polling** with detailed activity logs

## Supported Countries

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

## Prerequisites

### Docker Installation

This application requires Docker and Docker Compose to run. Follow the instructions below for your operating system.

#### Linux

For most Linux distributions, you can install Docker using the official convenience script:

```bash
# Download and run the Docker installation script
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add your user to the docker group (to run Docker without sudo)
sudo usermod -aG docker $USER

# Activate the changes to groups
newgrp docker

# Verify installation
docker --version
docker compose version
```

**Note:** You may need to log out and log back in for the group changes to take effect.

For detailed instructions and alternative installation methods, see the [official Docker documentation](https://docs.docker.com/engine/install/).

#### Windows with WSL2

Docker Desktop for Windows with WSL2 backend provides the best performance and compatibility.

**Prerequisites:**
- Windows 10 version 2004 or higher (Build 19041 or higher), or Windows 11
- WSL2 installed and configured

**Steps:**

1. **Install WSL2** (if not already installed):
   ```powershell
   # Run in PowerShell as Administrator
   wsl --install
   ```
   Restart your computer when prompted.

2. **Download and Install Docker Desktop:**
   - Download Docker Desktop from [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop)
   - Run the installer and follow the installation wizard
   - Ensure "Use WSL2 instead of Hyper-V" is selected during installation

3. **Configure Docker Desktop:**
   - Start Docker Desktop
   - Go to Settings → General
   - Ensure "Use the WSL2 based engine" is checked
   - Go to Settings → Resources → WSL Integration
   - Enable integration with your WSL2 distro (e.g., Ubuntu)
   - Click "Apply & Restart"

4. **Verify Installation in WSL2:**
   ```bash
   # Open your WSL2 terminal (e.g., Ubuntu)
   docker --version
   docker compose version
   ```

For more information, see the [Docker Desktop WSL2 backend documentation](https://docs.docker.com/desktop/wsl/).

## Quick Start Guide

### 1. Clone & Configure

```bash
git clone https://github.com/tubalainen/arma_reforger_base_map_generator_ng.git
cd arma_reforger_base_map_generator_ng

# Copy the example environment file
cp .env.example .env
```

The `.env` file is **optional for basic global use** — worldwide 30 m elevation
streams from AWS Open Data with no key required. Add credentials only for the
country APIs you want to use at higher resolution, or to set your timezone:

```bash
# Optional: backup elevation source if AWS Open Data is unavailable
OPENTOPOGRAPHY_API_KEY=your_key_here

# Optional: country-specific high-res elevation (see API Keys section)
LANTMATERIET_USERNAME=
LANTMATERIET_PASSWORD=
DATAFORSYNINGEN_TOKEN=
NLS_FINLAND_API_KEY=

# Optional: container timezone so server logs match the browser UI
# (defaults to UTC). Examples: Europe/Stockholm, America/New_York
TZ=Europe/Stockholm
```

### 2. Run

The application is published as a Docker image on GitHub Container Registry. Simply run:

```bash
docker compose up -d
```

Docker will automatically pull the latest image from `ghcr.io/tubalainen/arma_reforger_base_map_generator_ng:latest` and start the container. Then open **[http://localhost:8080](http://localhost:8080)** in your browser.

> **Building locally:** If you prefer to build from source instead of pulling the pre-built image, edit `docker-compose.yml`: comment out the `image:` line and uncomment the `build:` section, then run `docker compose up --build -d`.

### 3. Generate Your First Map

1. **Select an area** on the interactive map by clicking the rectangle or 1:1 square tool in the top-left (Enfusion only supports square / rectangular terrain)
2. **Set options** in the sidebar:
   - **Map Name** — letters, numbers, underscores (used as Enfusion project folder name)
   - **Heightmap Size** — 2049x2049 is a good default (2048 terrain faces at your chosen cell size)
   - **Grid Cell Size** — 2m is standard; 1m for high detail, 4-8m for large terrains
   - **Features** — toggle roads, water, forests, buildings, surface masks
3. **Click Generate** and watch the 13-step pipeline progress in real-time
4. **Download the ZIP** when complete

### 4. Import into Enfusion Workbench

The ZIP contains a ready-to-use Enfusion project structure with pre-configured `.gproj`, world files, layers, and a comprehensive **SETUP_GUIDE.md** with step-by-step Workbench import instructions tailored to your generated map.

See the [Output Files](#output-files) section below for the full file listing.

## API Keys

### Worldwide elevation needs no API key

Global 30 m elevation (Copernicus DEM) is read directly from the AWS Open
Data bucket `copernicus-dem-30m` — anonymous reads, no rate limit, no
registration. This is the default for every country except the six with
high-resolution national APIs below.

### Optional backup: OpenTopography

Used only if AWS Open Data is unavailable. Same Copernicus DEM 30m data
served from a different host. Also exposes SRTM 30 m (<60°N) and
ALOS World 3D 30 m as additional fallbacks.

**Registration:** [portal.opentopography.org](https://portal.opentopography.org/) (free)
**Env Variable:** `OPENTOPOGRAPHY_API_KEY`

### Optional: Country-Specific High-Resolution Sources

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

## Output Files

The generated ZIP package is organized into an Enfusion-ready project structure:

### Enfusion Project Files

| File | Format | Purpose |
|------|--------|---------|
| `addon.gproj` | Enfusion project | Workbench project file (open this in Enfusion Workbench) |
| `*.ent` | Enfusion entity | World entity with pre-configured terrain settings |
| `*_default.layer` | Enfusion layer | Layer index for the world |
| `*_managers.layer` | Enfusion layer | Game managers (camera, weather, sounds, map, etc.) |
| `*_gamemode.layer` | Enfusion layer | GameMode entry point |
| `*_roads.layer` | Enfusion layer | Road spline entities (one `SplineShapeEntity` per road segment) |
| `*_vegetation.layer` | Enfusion layer | One closed `SplineShapeEntity` per forest polygon — drag a Forest Generator (`FG_*`) prefab onto each |
| `*_water.layer` | Enfusion layer | One closed `SplineShapeEntity` per lake/pond/reservoir — drag a Lake Generator (`LG_*`) prefab onto each |
| `*.conf` | Enfusion config | Mission configuration |
| `*.meta` | Enfusion metadata | Resource metadata for each asset |
| `SETUP_GUIDE.md` | Markdown | Personalized step-by-step Workbench import guide |

### Sourcefiles (for Import)

| File | Format | Purpose |
|------|--------|---------|
| `heightmap.asc` | ESRI ASCII Grid | Enfusion heightmap import (preferred, lossless) |
| `heightmap.png` | 16-bit PNG | Enfusion heightmap import (alternative format) |
| `heightmap_preview.png` | 8-bit PNG | Visual preview of elevation |
| `surface_grass.png` | 8-bit grayscale PNG | Default grass/meadow surface (always present) |
| `surface_forest_floor.png` | 8-bit grayscale PNG | Deciduous forest floor (only if present in area) |
| `surface_pine_floor.png` | 8-bit grayscale PNG | Coniferous forest floor (only if present in area) |
| `surface_rock.png` | 8-bit grayscale PNG | Rock/alpine surface (steep slopes + above treeline) |
| `surface_asphalt.png` | 8-bit grayscale PNG | Paved roads + urban areas |
| `surface_gravel.png` | 8-bit grayscale PNG | Gravel/unpaved roads |
| `surface_dirt.png` | 8-bit grayscale PNG | Farmland and dirt paths |
| `surface_sand.png` | 8-bit grayscale PNG | Shoreline transition zone (skipped on landlocked maps) |
| `surface_water_edge.png` | 8-bit grayscale PNG | Outer transition ring around water polygons |
| `surface_preview.png` | RGB PNG | Combined surface preview visualization |
| `satellite_map.png` | PNG | Satellite texture overlay |

> Surfaces with no meaningful coverage are auto-omitted from the ZIP — a desert map won't ship a `surface_pine_floor.png`, a landlocked map won't ship `surface_sand.png`, etc.

### Reference Data

| File | Format | Purpose |
|------|--------|---------|
| `roads_enfusion.geojson` | GeoJSON | Roads with Enfusion prefab mapping |
| `roads_local.geojson` | GeoJSON | Roads in Enfusion local metre coordinates |
| `roads_splines.csv` | CSV | Road spline control points for World Editor |
| `osm_roads.geojson` | GeoJSON | Raw OSM road data with full tags |
| `osm_water.geojson` | GeoJSON | Raw OSM water features |
| `osm_forests.geojson` | GeoJSON | Raw OSM forest/woodland areas |
| `osm_buildings.geojson` | GeoJSON | Raw OSM building footprints |
| `osm_land_use.geojson` | GeoJSON | Raw OSM land use polygons |
| `features.json` | JSON | Processed feature data (water, forests, buildings with metadata) |
| `metadata.json` | JSON | Complete generation metadata + Enfusion import settings |

## Importing into Enfusion World Editor

The generated ZIP includes a pre-configured Enfusion project and a detailed **SETUP_GUIDE.md** personalized to your map. The high-level workflow:

1. Copy the generated addon folder to your Workbench addons directory
2. Open the `.gproj` in Enfusion Workbench — terrain entity and world layers are pre-configured
3. Import `heightmap.asc` via Terrain Tools → Import Heightmap
4. Batch-import `surface_*.png` masks via Terrain Tools → Import Surface Mask
5. Import `satellite_map.png` as the satellite texture overlay
6. Roads, vegetation and water layers are pre-populated with closed splines — drag a Forest Generator (`FG_*`) onto each forest spline and a Lake Generator (`LG_*`) onto each water spline

See the **SETUP_GUIDE.md** inside the ZIP for exact step-by-step instructions with pre-computed values for your terrain.

### Reference: "The Atlas 2" workflow

The generator is aligned with **The Atlas 2: Arma Reforger Terrain Creation Guide** by Jakerod (the community-standard manual workflow). A copy of the PDF lives at [`docs/Atlas2.pdf`](docs/Atlas2.pdf) and is the source of truth for canonical entity names, prefab paths, surface paint ordering, and required bootstrap entities.

## Security & Multi-User Support

The application includes built-in security features for safe deployment:

### Session Management

- **Automatic sessions**: Each user gets a secure session (256-bit cryptographic ID)
- **Job isolation**: Users can only access their own map generation jobs
- **24-hour expiration**: Sessions automatically expire after 24 hours of inactivity

### Security Features

| Feature | Description |
|---------|-------------|
| Rate Limiting | 60 requests/min general, 10 map generations/hour per IP |
| Input Validation | Polygon size limits, job ID format validation |
| Security Headers | CSP, X-Frame-Options, X-Content-Type-Options |
| SRI Hashes | Subresource integrity for all CDN resources |
| Non-root Container | Application runs as unprivileged user |

### Configuration

Security settings can be configured via environment variables in `.env`:

```bash
# CORS origins (for reverse proxy setup)
CORS_ORIGINS=https://your-domain.com,http://localhost:8080

# Rate limiting
RATE_LIMIT_REQUESTS_PER_MINUTE=60
RATE_LIMIT_GENERATE_PER_HOUR=10

# Trusted proxy IPs
FORWARDED_ALLOW_IPS=127.0.0.1
```

## Reverse Proxy Setup (nginx + Cloudflare)

For production deployment behind nginx and Cloudflare:

### Architecture

```
Internet → Cloudflare (DDoS/WAF) → nginx (rate limiting) → Docker container
```

### Quick Setup

1. **Configure Docker for localhost-only binding** (already default):
   ```yaml
   # docker-compose.yml
   ports:
     - "127.0.0.1:8080:8080"
   ```

2. **Copy the example nginx config**:
   ```bash
   sudo cp config/nginx/arma-map-generator.conf.example \
        /etc/nginx/sites-available/arma-map-generator.conf

   # Edit the file and update:
   # - server_name with your domain
   # - SSL certificate paths

   sudo ln -s /etc/nginx/sites-available/arma-map-generator.conf \
              /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```

3. **Configure Cloudflare** (recommended settings):
   - SSL/TLS: Full (Strict)
   - Always Use HTTPS: On
   - Minimum TLS: 1.2
   - Browser Integrity Check: On

4. **Update your `.env`**:
   ```bash
   CORS_ORIGINS=https://your-domain.com
   ```

### Local Network Access

To allow access from your local network (e.g., 192.168.x.x) alongside the reverse proxy:

```yaml
# docker-compose.yml
ports:
  - "127.0.0.1:8080:8080"      # For nginx
  - "192.168.1.100:8080:8080"  # For LAN (use your server's IP)
```

The application automatically detects local network requests and adjusts cookie security accordingly.

### Cloudflare Page Rules (Optional)

For optimal caching:

| Pattern | Setting |
|---------|---------|
| `*/static/*` | Cache Level: Cache Everything, Edge TTL: 1 day |
| `*/api/*` | Cache Level: Bypass |

## Docker Image

The application is published to GitHub Container Registry and automatically built on every push to `main`.

```bash
# Pull the latest image
docker pull ghcr.io/tubalainen/arma_reforger_base_map_generator_ng:latest

# Or use a specific version tag
docker pull ghcr.io/tubalainen/arma_reforger_base_map_generator_ng:v1.4.0
```

The `docker-compose.yml` is pre-configured to use the GHCR.io image. See the [Quick Start Guide](#quick-start-guide) for setup instructions.

## Tech Stack

- **Backend**: Python 3.11 + FastAPI + Uvicorn
- **GIS Processing**: GDAL, rasterio, shapely, pyproj, numpy, scipy, Pillow
- **Frontend**: Leaflet.js + Leaflet.Draw + Bootstrap 5
- **Container**: Docker (multi-stage build, non-root user)
- **Data Sources**:
  - Elevation: National WCS/STAC (SE/NO/EE/FI/DK/PL) → AWS COP30 Open Data → OpenTopography → SRTM → ALOS
  - Features: OSM Overpass API (4-mirror pool: Private.coffee, osm.ch, Kumi, overpass-api.de)
  - SE vector data: Lantmäteriet OGC API Features (Hydrografi, Marktäcke)
  - Satellite: Sentinel-2 Cloudless (global) + Lantmäteriet STAC Bild (Sweden, 2007–2025, 0.16 m/px) + Lantmäteriet WMS (Sweden, 2005 fallback)
  - Geocoding: Nominatim
- **CI/CD**: GitHub Actions → GHCR.io (auto-publish on push to main)
- **Security**: Session management, rate limiting (nginx + application), CORS, security headers, input validation
