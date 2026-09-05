# Installation & Setup

Docker prerequisites, configuration, and how to upgrade an existing deployment.

# Prerequisites

## Docker Installation

This application requires Docker and Docker Compose to run. Follow the instructions below for your operating system.

### Linux

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

### Windows with WSL2

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

# Quick Start Guide

## 1. Clone & Configure

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

## 2. Run

The application is published as a Docker image on GitHub Container Registry. Simply run:

```bash
docker compose up -d
```

Docker will automatically pull the latest image from `ghcr.io/tubalainen/arma_reforger_base_map_generator_ng:latest` and start the container. Then open **[http://localhost:8080](http://localhost:8080)** in your browser.

> **Building locally:** If you prefer to build from source instead of pulling the pre-built image, edit `docker-compose.yml`: comment out the `image:` line and uncomment the `build:` section, then run `docker compose up --build -d`.

## 3. Generate Your First Map

1. **Select an area** on the interactive map by clicking the rectangle or 1:1 square tool in the top-left (Enfusion only supports square / rectangular terrain)
2. **Set options** in the sidebar:
   - **Map Name** — letters, numbers, underscores (used as the Enfusion project folder name)
   - **Features** — toggle roads, water, forests, buildings and surface masks, plus road
     flattening and water levelling
   - The terrain grid is **derived from the square you drew** — cell size is fixed at 2 m
     (the Arma Reforger standard) and the square snaps to a whole number of 128-face tiles,
     up to 16384 x 16384 (32.8 km). There is nothing to pick.
3. **Click Generate** and watch the 13-step pipeline progress in real-time
4. **Download the ZIP** when complete

## 4. Import into Enfusion Workbench

The ZIP contains a ready-to-use Enfusion project structure with pre-configured `.gproj`, world files, layers, and a comprehensive **SETUP_GUIDE.md** with step-by-step Workbench import instructions tailored to your generated map.

See [Output Files](output-files.md) for the full file listing.

# Upgrading an existing deployment

Pulling a new image is not always enough: `docker-compose.yml` and `.env` live in
your working copy, so changes to them have to be picked up by hand.

```bash
git pull
docker compose pull
docker compose up -d
```

If you keep a customised `docker-compose.yml`, compare it against this
repository's version after upgrading. Changes that need merging:

| Version | Change to `docker-compose.yml` |
|---|---|
| v1.10.0 | Adds `overpass-local` and `overpass-local-init` services (profile `local-osm`), the `overpass_db` and `overpass_meta` volumes, and an `overpass_meta:/overpass_meta:ro` mount on the existing `arma-map-generator` service. All are inert unless you enable the profile — but without the mount on `arma-map-generator`, the UI cannot detect a country change. |
| v1.10.1 | `restart: on-failure:3` on `overpass-local` (was `unless-stopped`). Recommended, not required: a failed import leaves no database and the sidecar re-downloads the whole extract on each restart, so the old policy could loop. The PBF→bzip2 conversion this release adds needs **no** compose change — it ships in the app image. |

Also re-check `.env.example` after upgrading; new optional settings are added
there with comments explaining them.

# Docker Image

The application is published to GitHub Container Registry and automatically built on every push to `main`.

```bash
# Pull the latest image
docker pull ghcr.io/tubalainen/arma_reforger_base_map_generator_ng:latest

# Or use a specific version tag
docker pull ghcr.io/tubalainen/arma_reforger_base_map_generator_ng:v1.7.0
```

The `docker-compose.yml` is pre-configured to use the GHCR.io image. See the [Quick Start Guide](#quick-start-guide) for setup instructions.

---

[← Back to the README](../README.md)
