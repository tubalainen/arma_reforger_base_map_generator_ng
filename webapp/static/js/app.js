/**
 * Arma Reforger Base Map Generator - Frontend Application
 *
 * Interactive map with polygon drawing for area selection,
 * generation controls, and result display.
 */

// ===========================================================================
// Map initialization
// ===========================================================================

const map = L.map('map', {
    center: [60.0, 18.0],  // Default: Nordic region
    zoom: 5,
    zoomControl: true,
});

// Base tile layers
const osmLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
});

const satelliteLayer = L.tileLayer(
    'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2021_3857/default/GoogleMapsCompatible/{z}/{y}/{x}.jpg', {
    attribution: '&copy; <a href="https://s2maps.eu">Sentinel-2 cloudless by EOX</a>',
    maxZoom: 15,
});

// Add default layer
osmLayer.addTo(map);

// Layer control
L.control.layers({
    'OpenStreetMap': osmLayer,
    'Satellite (Sentinel-2)': satelliteLayer,
}, null, { position: 'topright' }).addTo(map);

// ===========================================================================
// Polygon drawing
// ===========================================================================

const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
    position: 'topleft',
    draw: {
        polygon: {
            allowIntersection: false,
            drawError: { color: '#f85149', timeout: 1000 },
            shapeOptions: {
                color: '#26cd4d',
                fillColor: '#26cd4d',
                fillOpacity: 0.15,
                weight: 2,
            },
        },
        rectangle: {
            shapeOptions: {
                color: '#26cd4d',
                fillColor: '#26cd4d',
                fillOpacity: 0.15,
                weight: 2,
            },
        },
        // Disable other draw tools
        polyline: false,
        circle: false,
        circlemarker: false,
        marker: false,
    },
    edit: {
        featureGroup: drawnItems,
        remove: true,
    },
});
map.addControl(drawControl);

// ===========================================================================
// State
// ===========================================================================

let currentPolygon = null;
let currentPolygonCoords = null;
let currentJobId = null;
let currentAccessToken = null;  // Access token for downloads
let pollInterval = null;
let lastLoggedStepCount = 0;
let lastCurrentStep = '';
let lastDisplayedLogCount = 0;

// ===========================================================================
// Event handlers
// ===========================================================================

map.on(L.Draw.Event.CREATED, function (event) {
    // Clear previous polygon
    drawnItems.clearLayers();

    const layer = event.layer;
    drawnItems.addLayer(layer);
    currentPolygon = layer;

    // Extract coordinates as [lng, lat] pairs
    let coords;
    if (event.layerType === 'rectangle') {
        const bounds = layer.getBounds();
        coords = [
            [bounds.getWest(), bounds.getSouth()],
            [bounds.getEast(), bounds.getSouth()],
            [bounds.getEast(), bounds.getNorth()],
            [bounds.getWest(), bounds.getNorth()],
            [bounds.getWest(), bounds.getSouth()],
        ];
    } else {
        const latLngs = layer.getLatLngs()[0];
        coords = latLngs.map(ll => [ll.lng, ll.lat]);
        // Close polygon
        coords.push([latLngs[0].lng, latLngs[0].lat]);
    }

    currentPolygonCoords = coords;
    onPolygonSelected(coords);
});

map.on(L.Draw.Event.DELETED, function () {
    currentPolygon = null;
    currentPolygonCoords = null;
    onPolygonCleared();
});

// ===========================================================================
// UI handlers
// ===========================================================================

document.getElementById('btn-generate').addEventListener('click', startGeneration);
document.getElementById('btn-clear').addEventListener('click', clearSelection);
document.getElementById('btn-close-results').addEventListener('click', closeResults);

// Console toggle handler
document.getElementById('btn-toggle-console').addEventListener('click', function() {
    const consoleLog = document.getElementById('console-log');
    const toggleBtn = this;
    const icon = toggleBtn.querySelector('i');

    if (consoleLog.classList.contains('collapsed')) {
        consoleLog.classList.remove('collapsed');
        icon.className = 'bi bi-chevron-down';
    } else {
        consoleLog.classList.add('collapsed');
        icon.className = 'bi bi-chevron-up';
    }
});

// Update terrain size display when options change
document.getElementById('heightmap-size').addEventListener('change', updateTerrainSizeDisplay);
document.getElementById('grid-resolution').addEventListener('change', updateTerrainSizeDisplay);

const MAX_MAP_EXTENT_KM = 20; // must match MAX_MAP_EXTENT_M in config/terrain.py

function onPolygonSelected(coords) {
    document.getElementById('selection-info').classList.remove('d-none');
    document.getElementById('no-selection').classList.add('d-none');

    // Compute bounding box
    const lngs = coords.map(c => c[0]);
    const lats = coords.map(c => c[1]);
    const west = Math.min(...lngs);
    const east = Math.max(...lngs);
    const south = Math.min(...lats);
    const north = Math.max(...lats);

    document.getElementById('info-bbox').textContent =
        `${south.toFixed(4)}, ${west.toFixed(4)} - ${north.toFixed(4)}, ${east.toFixed(4)}`;

    // Estimate size in km
    const dLat = north - south;
    const dLng = east - west;
    const latKm = dLat * 111;
    const lngKm = dLng * 111 * Math.cos((north + south) / 2 * Math.PI / 180);
    document.getElementById('info-size').textContent =
        `~${lngKm.toFixed(1)} x ${latKm.toFixed(1)} km`;

    // Check area limit
    const warning = document.getElementById('area-warning');
    const warningText = document.getElementById('area-warning-text');
    if (lngKm > MAX_MAP_EXTENT_KM || latKm > MAX_MAP_EXTENT_KM) {
        warningText.textContent =
            `Area too large (${lngKm.toFixed(1)} x ${latKm.toFixed(1)} km). Max ${MAX_MAP_EXTENT_KM} x ${MAX_MAP_EXTENT_KM} km.`;
        warning.classList.remove('d-none');
        document.getElementById('btn-generate').disabled = true;
    } else {
        warning.classList.add('d-none');
        document.getElementById('btn-generate').disabled = false;
    }

    // Detect countries (quick bbox check)
    detectCountries(coords);
}

function onPolygonCleared() {
    document.getElementById('selection-info').classList.add('d-none');
    document.getElementById('no-selection').classList.remove('d-none');
    document.getElementById('btn-generate').disabled = true;
    document.getElementById('info-countries').textContent = '-';
    document.getElementById('country-sources').innerHTML = '';
}

function clearSelection() {
    drawnItems.clearLayers();
    currentPolygon = null;
    currentPolygonCoords = null;
    onPolygonCleared();
    closeResults();
}

function updateTerrainSizeDisplay() {
    const vertices = parseInt(document.getElementById('heightmap-size').value);
    const res = parseFloat(document.getElementById('grid-resolution').value);
    const faces = vertices - 1;
    const terrainM = faces * res;
    document.getElementById('terrain-size-display').innerHTML =
        `${faces} faces at ${res}m = <strong>${terrainM}m x ${terrainM}m</strong> terrain`;
}

// ===========================================================================
// Country detection
// ===========================================================================

async function detectCountries(coords) {
    try {
        const resp = await fetch('/api/detect-countries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ polygon: coords }),
        });
        const data = await resp.json();

        if (data.countries && data.countries.length > 0) {
            document.getElementById('info-countries').textContent =
                data.countries.join(', ');

            // Update data sources display
            updateDataSourcesDisplay(data.countries, data.primary_country);
        }
    } catch (err) {
        console.error('Country detection failed:', err);
        document.getElementById('info-countries').textContent = 'Detection failed';
    }
}

async function updateDataSourcesDisplay(countries, primaryCountry) {
    const container = document.getElementById('country-sources');
    container.innerHTML = '';

    const countryNames = {
        'SE': 'Sweden (Lantmateriet)',
        'NO': 'Norway (Kartverket)',
        'DK': 'Denmark (Dataforsyningen)',
        'FI': 'Finland (NLS/MML)',
        'EE': 'Estonia (Maa-amet)',
        'LV': 'Latvia (LGIA)',
        'LT': 'Lithuania (GIS-Centras)',
    };

    // Fetch actual data source status from backend
    let dataSourcesStatus = {};
    try {
        const resp = await fetch('/api/data-sources');
        const data = await resp.json();
        dataSourcesStatus = data.countries || {};
    } catch (err) {
        console.error('Failed to fetch data sources status:', err);
    }

    countries.forEach(code => {
        const name = countryNames[code] || code;
        const sourceInfo = dataSourcesStatus[code];

        if (!sourceInfo) {
            // Unknown country - show as unavailable
            return;
        }

        // Display based on actual status from backend
        if (sourceInfo.status === 'available' || sourceInfo.status === 'configured') {
            const icon = sourceInfo.status === 'configured'
                ? '<i class="bi bi-check-circle-fill"></i>'
                : '<i class="bi bi-check-circle"></i>';
            container.innerHTML += `<div class="mt-1">${icon} ${name} DEM (${sourceInfo.resolution_m}m)</div>`;
        } else if (sourceInfo.status === 'api_key_required') {
            container.innerHTML += `<div class="mt-1"><i class="bi bi-key"></i> ${name} DEM (API key needed)</div>`;
        } else {
            container.innerHTML += `<div class="mt-1"><i class="bi bi-x-circle"></i> ${name} DEM (${sourceInfo.note || 'unavailable'})</div>`;
        }
    });
}

// ===========================================================================
// Generation
// ===========================================================================

async function startGeneration() {
    if (!currentPolygonCoords) {
        alert('Please draw an area on the map first.');
        return;
    }

    // Read and sanitize map name
    const rawMapName = document.getElementById('map-name').value.trim();
    const mapName = rawMapName.replace(/[^A-Za-z0-9_]/g, '').substring(0, 32);

    const options = {
        heightmap_size: parseInt(document.getElementById('heightmap-size').value),
        grid_resolution: parseFloat(document.getElementById('grid-resolution').value),
        features: {
            roads: document.getElementById('opt-roads').checked,
            water: document.getElementById('opt-water').checked,
            forests: document.getElementById('opt-forests').checked,
            buildings: document.getElementById('opt-buildings').checked,
            surface_masks: document.getElementById('opt-surface-masks').checked,
            road_flatten: document.getElementById('opt-road-flatten').checked,
            water_level: document.getElementById('opt-water-level').checked,
        },
    };

    // Show progress overlay
    showProgress();

    try {
        const resp = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                polygon: currentPolygonCoords,
                options: options,
                map_name: mapName || undefined,
            }),
        });
        const data = await resp.json();

        if (data.job_id) {
            currentJobId = data.job_id;
            currentAccessToken = data.access_token;  // Store access token for download
            startPolling(data.job_id);
        } else {
            hideProgress();
            alert('Failed to start generation: ' + (data.error || 'Unknown error'));
        }
    } catch (err) {
        hideProgress();
        alert('Failed to start generation: ' + err.message);
    }
}

function startPolling(jobId) {
    if (pollInterval) clearInterval(pollInterval);

    pollInterval = setInterval(async () => {
        try {
            // Use public status endpoint that doesn't require auth
            const resp = await fetch(`/status/${jobId}`);
            const job = await resp.json();

            updateProgress(job);

            if (job.status === 'completed') {
                addConsoleLog('✓ Map generation completed successfully!', 'success');
                clearInterval(pollInterval);
                pollInterval = null;
                hideProgress();
                showResults(job);
            } else if (job.status === 'failed') {
                const errorMsg = job.errors.join('; ') || 'Unknown error';
                addConsoleLog(`✗ Generation failed: ${errorMsg}`, 'error');
                clearInterval(pollInterval);
                pollInterval = null;
                hideProgress();
                alert('Generation failed: ' + errorMsg);
            }
        } catch (err) {
            console.error('Polling error:', err);
        }
    }, 1500);
}

// ===========================================================================
// Console logging
// ===========================================================================

function addConsoleLog(message, level = 'info') {
    const consoleLog = document.getElementById('console-log');
    const entry = document.createElement('div');
    entry.className = 'console-entry';

    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });
    const levelLabel = level.toUpperCase().padEnd(7, ' ');

    entry.innerHTML = `
        <span class="console-timestamp">[${timestamp}]</span>
        <span class="console-level ${level}">${levelLabel}</span>
        <span class="console-message">${message}</span>
    `;

    consoleLog.appendChild(entry);

    // Auto-scroll to bottom
    consoleLog.scrollTop = consoleLog.scrollHeight;
}

function clearConsoleLog() {
    document.getElementById('console-log').innerHTML = '';
}

// ===========================================================================
// Progress display
// ===========================================================================

function showProgress() {
    document.getElementById('progress-overlay').classList.remove('d-none');
    document.getElementById('results-panel').classList.add('d-none');
    document.getElementById('btn-generate').disabled = true;
    document.getElementById('progress-bar').style.width = '0%';
    document.getElementById('progress-bar').textContent = '0%';
    document.getElementById('progress-step').textContent = 'Starting...';
    document.getElementById('progress-steps').innerHTML = '';
    clearConsoleLog();
    lastLoggedStepCount = 0;
    lastCurrentStep = '';
    lastDisplayedLogCount = 0;
    addConsoleLog('Map generation job started', 'info');
}

function hideProgress() {
    document.getElementById('progress-overlay').classList.add('d-none');
    document.getElementById('btn-generate').disabled = false;
}

function updateProgress(job) {
    const bar = document.getElementById('progress-bar');
    bar.style.width = job.progress + '%';
    bar.textContent = job.progress + '%';

    // Display new log messages from job.logs
    if (job.logs && Array.isArray(job.logs)) {
        // Add only new log entries that we haven't displayed yet
        for (let i = lastDisplayedLogCount; i < job.logs.length; i++) {
            const logEntry = job.logs[i];
            addConsoleLog(logEntry.message, logEntry.level);
        }
        lastDisplayedLogCount = job.logs.length;
    }

    document.getElementById('progress-step').textContent = job.current_step;

    // Update completed steps and add new ones to console
    const stepsContainer = document.getElementById('progress-steps');
    stepsContainer.innerHTML = '';

    // Log only newly completed steps
    if (job.steps_completed.length > lastLoggedStepCount) {
        for (let i = lastLoggedStepCount; i < job.steps_completed.length; i++) {
            const step = job.steps_completed[i];
            let consoleMessage = '';

            switch (step.step) {
                case 'country_detection':
                    consoleMessage = `✓ Detected countries: ${step.countries.join(', ')} (CRS: ${step.crs})`;
                    break;
                case 'elevation_download':
                    consoleMessage = `✓ Downloaded elevation data from ${step.source} (${step.resolution_m}m resolution)`;
                    break;
                case 'osm_features':
                    const counts = step.feature_counts;
                    consoleMessage = `✓ Fetched OSM features: ${counts.roads || 0} roads, ${counts.water || 0} water, ${counts.forests || 0} forests, ${counts.buildings || 0} buildings`;
                    break;
                case 'heightmap':
                    consoleMessage = `✓ Generated heightmap: ${step.dimensions} (${step.elevation_range})`;
                    break;
                case 'surface_masks':
                    consoleMessage = `✓ Created ${step.mask_count} surface masks: ${step.surfaces.join(', ')}`;
                    break;
                case 'road_processing':
                    consoleMessage = `✓ Processed ${step.road_count} road segments`;
                    break;
                case 'feature_extraction':
                    consoleMessage = `✓ Extracted features: ${step.summary.lakes || 0} lakes, ${step.summary.rivers || 0} rivers, ${step.summary.forest_areas || 0} forests, ${step.summary.buildings || 0} buildings`;
                    break;
                case 'coordinate_transform':
                    consoleMessage = `✓ Coordinate transformer set up (${step.method || 'auto'}, CRS: ${step.crs || 'N/A'})`;
                    break;
                case 'enfusion_project':
                    consoleMessage = `✓ Generated Enfusion project: ${step.map_name || 'unnamed'} (${step.files_created || 0} files)`;
                    break;
                case 'setup_guide':
                    consoleMessage = `✓ Generated SETUP_GUIDE.md`;
                    break;
                case 'export_organized':
                    consoleMessage = `✓ Export package organized and ZIP created`;
                    break;
            }

            if (consoleMessage) {
                addConsoleLog(consoleMessage, 'success');
            }
        }
        lastLoggedStepCount = job.steps_completed.length;
    }

    // Update visual step list
    for (const step of job.steps_completed) {
        const div = document.createElement('div');
        div.className = 'step-item completed';

        let detail = '';
        switch (step.step) {
            case 'country_detection':
                detail = `Countries: ${step.countries.join(', ')}`;
                break;
            case 'elevation_download':
                detail = `Elevation: ${step.source} (${step.resolution_m}m)`;
                break;
            case 'osm_features':
                const counts = step.feature_counts;
                detail = `Features: ${counts.roads || 0} roads, ${counts.water || 0} water, ${counts.forests || 0} forests, ${counts.buildings || 0} buildings`;
                break;
            case 'heightmap':
                detail = `Heightmap: ${step.dimensions} (${step.elevation_range})`;
                break;
            case 'surface_masks':
                detail = `Surface masks: ${step.mask_count} masks`;
                break;
            case 'road_processing':
                detail = `Roads: ${step.road_count} segments`;
                break;
            case 'feature_extraction':
                detail = `Features: ${step.summary.lakes || 0} lakes, ${step.summary.rivers || 0} rivers, ${step.summary.forest_areas || 0} forests, ${step.summary.buildings || 0} buildings`;
                break;
            case 'coordinate_transform':
                detail = `Coordinates: ${step.method || 'auto'} (${step.crs || 'N/A'})`;
                break;
            case 'enfusion_project':
                detail = `Enfusion project: ${step.map_name || 'unnamed'} (${step.files_created || 0} files)`;
                break;
            case 'setup_guide':
                detail = `Setup guide generated`;
                break;
            case 'export_organized':
                detail = `Export organized & ZIP created`;
                break;
        }

        div.innerHTML = `<i class="bi bi-check-circle-fill"></i> ${detail}`;
        stepsContainer.appendChild(div);
    }
}

// ===========================================================================
// Results display
// ===========================================================================

function showResults(job) {
    const panel = document.getElementById('results-panel');
    panel.classList.remove('d-none');

    if (!job.result) return;

    const result = job.result;
    const meta = result.metadata;

    // Preview images with error handling and retry logic
    const hPreview = document.getElementById('preview-heightmap');
    const sPreview = document.getElementById('preview-surface');

    // Helper function to load preview with retry on error
    function loadPreviewWithRetry(imgElement, previewType, maxRetries = 3, retryDelay = 1000) {
        let attemptCount = 0;

        const tryLoad = () => {
            attemptCount++;
            const timestamp = Date.now();
            // Note: Preview endpoints require session cookie, not accessible via Cloudflare
            const previewUrl = `/api/job/${job.job_id}/preview/${previewType}?t=${timestamp}`;

            // Clear previous handlers
            imgElement.onerror = null;
            imgElement.onload = null;

            // Set up error handler with retry logic
            imgElement.onerror = () => {
                if (attemptCount < maxRetries) {
                    console.log(`Preview ${previewType} failed to load (attempt ${attemptCount}/${maxRetries}), retrying in ${retryDelay}ms...`);
                    setTimeout(tryLoad, retryDelay);
                } else {
                    console.error(`Preview ${previewType} failed to load after ${maxRetries} attempts`);
                    // Show fallback message
                    imgElement.alt = `${previewType.charAt(0).toUpperCase() + previewType.slice(1)} preview unavailable`;
                    imgElement.style.display = 'none';
                    const parent = imgElement.parentElement;
                    if (parent) {
                        const fallback = document.createElement('div');
                        fallback.className = 'alert alert-warning small mb-0';
                        fallback.innerHTML = `<i class="bi bi-exclamation-triangle"></i> Preview image not available`;
                        parent.appendChild(fallback);
                    }
                }
            };

            // Set up success handler
            imgElement.onload = () => {
                console.log(`Preview ${previewType} loaded successfully`);
                imgElement.style.display = 'block';
            };

            // Start loading
            imgElement.src = previewUrl;
        };

        tryLoad();
    }

    // Load both preview images with retry logic
    loadPreviewWithRetry(hPreview, 'heightmap');
    loadPreviewWithRetry(sPreview, 'surface');

    // Stats table
    const statsDiv = document.getElementById('result-stats');
    const mapNameRow = meta.map_name
        ? `<tr><td>Project Name</td><td>${meta.map_name}</td></tr>`
        : '';
    const coordRow = meta.coordinate_transform
        ? `<tr><td>Projection</td><td>${meta.coordinate_transform.method} (${meta.coordinate_transform.crs})</td></tr>`
        : '';
    statsDiv.innerHTML = `
        <table class="table table-sm table-dark mb-0">
            ${mapNameRow}
            <tr><td>Terrain Size</td><td>${meta.heightmap.terrain_size_m}</td></tr>
            <tr><td>Elevation</td><td>${meta.elevation.min_elevation_m.toFixed(1)}m - ${meta.elevation.max_elevation_m.toFixed(1)}m</td></tr>
            <tr><td>Elevation Source</td><td>${meta.elevation.source}</td></tr>
            <tr><td>Roads</td><td>${meta.roads.total_segments} segments</td></tr>
            <tr><td>Surface Masks</td><td>${meta.surface_masks.count} (${meta.surface_masks.surfaces.join(', ')})</td></tr>
            <tr><td>Countries</td><td>${meta.input.countries.join(', ')}</td></tr>
            <tr><td>CRS</td><td>${meta.input.crs}</td></tr>
            ${coordRow}
        </table>
    `;

    // File list
    const filesDiv = document.getElementById('result-files');
    filesDiv.innerHTML = '<strong>Output:</strong>';
    if (result.files) {
        const filesGrid = document.createElement('div');
        filesGrid.className = 'files-grid';
        result.files.forEach(f => {
            const fileItem = document.createElement('div');
            fileItem.className = 'file-item';
            fileItem.innerHTML = `<span>${f}</span>`;
            filesGrid.appendChild(fileItem);
        });
        filesDiv.appendChild(filesGrid);
    }

    // Update retention warning with time remaining if available
    if (job.retention && job.retention.minutes_remaining) {
        const warningDiv = document.querySelector('.alert-warning');
        if (warningDiv) {
            warningDiv.innerHTML = `
                <i class="bi bi-clock-history"></i>
                <strong>Note:</strong> Generated files will be automatically deleted in approximately
                <strong>${job.retention.minutes_remaining} minutes</strong>.
                Please download your map package promptly.
            `;
        }
    }

    // Download button - use new download endpoint with access token
    document.getElementById('btn-download').onclick = () => {
        if (currentAccessToken) {
            window.location.href = `/download/${job.job_id}?token=${encodeURIComponent(currentAccessToken)}`;
        } else {
            // Fallback to session-based download
            window.location.href = `/download/${job.job_id}`;
        }
    };
}

function closeResults() {
    document.getElementById('results-panel').classList.add('d-none');
}

// ===========================================================================
// Initialization
// ===========================================================================

// Update terrain size display on load
updateTerrainSizeDisplay();

// Fetch and display data sources status
async function fetchDataSources() {
    try {
        const resp = await fetch('/api/data-sources');
        const data = await resp.json();
        // Could update the sidebar with more detailed source info
        console.log('Data sources:', data);
    } catch (err) {
        console.log('Could not fetch data sources status');
    }
}

// Fetch and display application version
async function fetchVersion() {
    try {
        const resp = await fetch('/api/health');
        const data = await resp.json();
        if (data.version) {
            document.getElementById('app-version').textContent = data.version;
        }
    } catch (err) {
        console.log('Could not fetch version info');
    }
}

fetchDataSources();
fetchVersion();
