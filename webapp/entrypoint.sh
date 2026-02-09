#!/bin/bash
set -e

# Create images directory if it doesn't exist
mkdir -p /app/static/images

# Download Leaflet.Draw icon files if they don't exist
ICONS_BASE_URL="https://cdn.jsdelivr.net/npm/leaflet-draw@1.0.4/dist/images"

echo "Checking for Leaflet.Draw icon files..."

if [ ! -f /app/static/images/spritesheet.svg ]; then
    echo "Downloading spritesheet.svg..."
    curl -f -s -o /app/static/images/spritesheet.svg "${ICONS_BASE_URL}/spritesheet.svg" || echo "Warning: Failed to download spritesheet.svg"
fi

if [ ! -f /app/static/images/spritesheet-2x.png ]; then
    echo "Downloading spritesheet-2x.png..."
    curl -f -s -o /app/static/images/spritesheet-2x.png "${ICONS_BASE_URL}/spritesheet-2x.png" || echo "Warning: Failed to download spritesheet-2x.png"
fi

if [ ! -f /app/static/images/spritesheet.png ]; then
    echo "Downloading spritesheet.png..."
    curl -f -s -o /app/static/images/spritesheet.png "${ICONS_BASE_URL}/spritesheet.png" || echo "Warning: Failed to download spritesheet.png"
fi

# Download Leaflet core icon files (for layer control, markers, etc.)
LEAFLET_ICONS_URL="https://unpkg.com/leaflet@1.9.4/dist/images"

echo "Checking for Leaflet core icon files..."

if [ ! -f /app/static/images/layers.png ]; then
    echo "Downloading layers.png..."
    curl -f -s -o /app/static/images/layers.png "${LEAFLET_ICONS_URL}/layers.png" || echo "Warning: Failed to download layers.png"
fi

if [ ! -f /app/static/images/layers-2x.png ]; then
    echo "Downloading layers-2x.png..."
    curl -f -s -o /app/static/images/layers-2x.png "${LEAFLET_ICONS_URL}/layers-2x.png" || echo "Warning: Failed to download layers-2x.png"
fi

if [ ! -f /app/static/images/marker-icon.png ]; then
    echo "Downloading marker-icon.png..."
    curl -f -s -o /app/static/images/marker-icon.png "${LEAFLET_ICONS_URL}/marker-icon.png" || echo "Warning: Failed to download marker-icon.png"
fi

if [ ! -f /app/static/images/marker-icon-2x.png ]; then
    echo "Downloading marker-icon-2x.png..."
    curl -f -s -o /app/static/images/marker-icon-2x.png "${LEAFLET_ICONS_URL}/marker-icon-2x.png" || echo "Warning: Failed to download marker-icon-2x.png"
fi

if [ ! -f /app/static/images/marker-shadow.png ]; then
    echo "Downloading marker-shadow.png..."
    curl -f -s -o /app/static/images/marker-shadow.png "${LEAFLET_ICONS_URL}/marker-shadow.png" || echo "Warning: Failed to download marker-shadow.png"
fi

echo "Icon check complete. Starting application..."

# Execute the main command (uvicorn)
exec "$@"
