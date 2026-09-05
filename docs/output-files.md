# Output Files

The generated ZIP package is organized into an Enfusion-ready project structure:

## Enfusion Project Files

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
| `*_buildings.layer` | Enfusion layer | One positioned `Building_*.et` prefab instance per OSM building (rotation-aligned to longest wall) — no spline wiring needed |
| `*.conf` | Enfusion config | Mission configuration |
| `*.meta` | Enfusion metadata | Resource metadata for each asset |
| `SETUP_GUIDE.md` | Markdown | Personalized step-by-step Workbench import guide |

## Sourcefiles (for Import)

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

## Reference Data

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

---

[← Back to the README](../README.md)
