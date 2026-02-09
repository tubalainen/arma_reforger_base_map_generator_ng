"""
Feature extraction service.

Extracts and processes geographic features for Arma Reforger:
- Water bodies (lakes, rivers, coastline)
- Forests (areas, type, density)
- Buildings (footprints, type, height)
- Land use classification
"""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def extract_water_features(water_data: dict, country_code: str = "UNKNOWN", job = None) -> dict:
    """
    Process water features for Enfusion export.

    Classifies water bodies and generates placement data:
    - Lakes -> flat water surface entities
    - Rivers -> water flow splines
    - Coastline -> ocean entity boundaries
    - Wetlands -> special surface areas

    Args:
        water_data: GeoJSON FeatureCollection of water features
        country_code: For country-specific rules

    Returns:
        Processed water feature data
    """
    if not water_data or not water_data.get("features"):
        return {"lakes": [], "rivers": [], "coastlines": [], "wetlands": [], "stats": {}}

    lakes = []
    rivers = []
    coastlines = []
    wetlands = []

    for feature in water_data["features"]:
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        water_type = props.get("water_type", "")
        geom_type = geom.get("type", "")

        if water_type in ("lake", "pond", "reservoir", "water", "basin"):
            if geom_type in ("Polygon", "MultiPolygon"):
                lakes.append({
                    "name": props.get("name", f"Lake_{props.get('osm_id', 'unknown')}"),
                    "osm_id": props.get("osm_id"),
                    "type": water_type,
                    "geometry": geom,
                    "area_estimate": _estimate_polygon_area(geom),
                })

        elif water_type in ("river", "stream", "canal"):
            if geom_type == "LineString":
                rivers.append({
                    "name": props.get("name", f"River_{props.get('osm_id', 'unknown')}"),
                    "osm_id": props.get("osm_id"),
                    "type": water_type,
                    "width_estimate": _estimate_river_width(water_type),
                    "geometry": geom,
                    "intermittent": props.get("intermittent", "no") == "yes",
                })

        elif water_type == "coastline":
            coastlines.append({
                "osm_id": props.get("osm_id"),
                "geometry": geom,
            })

        elif water_type == "wetland":
            wetlands.append({
                "name": props.get("name", ""),
                "osm_id": props.get("osm_id"),
                "geometry": geom,
            })

    stats = {
        "lakes": len(lakes),
        "rivers": len(rivers),
        "coastlines": len(coastlines),
        "wetlands": len(wetlands),
    }

    # Calculate total area of lakes
    total_lake_area = sum(lake.get("area_estimate", 0) for lake in lakes)

    logger.info(
        f"Extracted water features: {stats['lakes']} lakes ({total_lake_area:.1f} km²), "
        f"{stats['rivers']} rivers/streams, {stats['coastlines']} coastline segments, "
        f"{stats['wetlands']} wetlands"
    )

    if job:
        job.add_log(
            f"✓ Water extraction: {stats['lakes']} lakes ({total_lake_area:.1f} km²), "
            f"{stats['rivers']} rivers, {stats['coastlines']} coastline segments, "
            f"{stats['wetlands']} wetlands",
            "success"
        )

    return {
        "lakes": lakes,
        "rivers": rivers,
        "coastlines": coastlines,
        "wetlands": wetlands,
        "stats": stats,
    }


def _estimate_river_width(water_type: str) -> float:
    """Estimate river width based on type."""
    widths = {
        "river": 15.0,
        "stream": 3.0,
        "canal": 8.0,
        "ditch": 1.5,
        "drain": 2.0,
    }
    return widths.get(water_type, 5.0)


def _estimate_polygon_area(geom: dict) -> float:
    """
    Rough area estimate in km^2 from GeoJSON polygon (WGS84 degrees).

    Uses the polygon centroid latitude for the longitude scale factor.
    At latitude phi: 1 deg lon ≈ 111.320 * cos(phi) km, 1 deg lat ≈ 110.540 km.
    """
    try:
        import math
        from shapely.geometry import shape
        poly = shape(geom)
        center_lat = poly.centroid.y
        km_per_deg_lon = 111.320 * math.cos(math.radians(center_lat))
        km_per_deg_lat = 110.540
        # poly.area is in degree^2, convert to km^2
        return poly.area * km_per_deg_lon * km_per_deg_lat
    except Exception:
        return 0


def extract_forest_features(
    forest_data: dict,
    country_code: str = "UNKNOWN",
    job = None,
) -> dict:
    """
    Process forest areas for Enfusion vegetation placement.

    Classifies forest type and estimates density:
    - Coniferous (pine, spruce) -> Nordic boreal forests
    - Deciduous (birch, beech, oak) -> Southern areas
    - Mixed -> Transition zones

    Args:
        forest_data: GeoJSON FeatureCollection of forests
        country_code: For biome-specific rules

    Returns:
        Processed forest data with type and density info
    """
    if not forest_data or not forest_data.get("features"):
        return {"forests": [], "stats": {}}

    forests = []

    # Country-specific default forest type
    default_forest_type = {
        "NO": "coniferous",
        "SE": "coniferous",
        "FI": "coniferous",
        "DK": "deciduous",
        "EE": "mixed",
        "LV": "mixed",
        "LT": "mixed",
    }.get(country_code, "mixed")

    for feature in forest_data["features"]:
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        geom_type = geom.get("type", "")

        if geom_type not in ("Polygon", "MultiPolygon"):
            continue

        # Determine forest type
        leaf_type = props.get("leaf_type", "")
        area_type = props.get("type", "")

        if leaf_type == "needleleaved":
            forest_type = "coniferous"
        elif leaf_type == "broadleaved":
            forest_type = "deciduous"
        elif leaf_type == "mixed":
            forest_type = "mixed"
        elif area_type == "scrub":
            forest_type = "scrub"
        elif area_type == "heath":
            forest_type = "heath"
        else:
            forest_type = default_forest_type

        # Estimate tree density (0-1 scale)
        if forest_type in ("scrub", "heath"):
            density = 0.3
        elif forest_type == "coniferous":
            density = 0.7
        elif forest_type == "deciduous":
            density = 0.6
        else:
            density = 0.65

        # Enfusion vegetation species mapping
        species = _get_species_for_type(forest_type, country_code)

        forests.append({
            "name": props.get("name", ""),
            "osm_id": props.get("osm_id"),
            "forest_type": forest_type,
            "density": density,
            "species": species,
            "geometry": geom,
            "area_km2": _estimate_polygon_area(geom),
        })

    stats = {
        "total_areas": len(forests),
        "by_type": {},
    }
    for f in forests:
        t = f["forest_type"]
        stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

    # Calculate total forest area
    total_forest_area = sum(f.get("area_km2", 0) for f in forests)

    logger.info(
        f"Extracted {stats['total_areas']} forest areas ({total_forest_area:.1f} km²). "
        f"By type: {stats['by_type']}"
    )

    if job:
        type_details = ", ".join([f"{k}: {v}" for k, v in stats['by_type'].items()])
        job.add_log(
            f"✓ Forest extraction: {stats['total_areas']} areas ({total_forest_area:.1f} km²) "
            f"by type ({type_details})",
            "success"
        )

    return {
        "forests": forests,
        "stats": stats,
    }


def _get_species_for_type(forest_type: str, country_code: str) -> list:
    """Get tree species list for a given forest type and country."""
    species_map = {
        ("coniferous", "NO"): ["spruce", "pine", "juniper"],
        ("coniferous", "SE"): ["spruce", "pine"],
        ("coniferous", "FI"): ["spruce", "pine", "birch"],
        ("deciduous", "NO"): ["birch", "oak", "elm"],
        ("deciduous", "SE"): ["birch", "beech", "oak"],
        ("deciduous", "FI"): ["birch", "aspen"],
        ("deciduous", "DK"): ["beech", "oak", "ash"],
        ("mixed", "NO"): ["spruce", "pine", "birch"],
        ("mixed", "SE"): ["spruce", "pine", "birch", "oak"],
        ("mixed", "FI"): ["spruce", "pine", "birch"],
        ("mixed", "EE"): ["pine", "spruce", "birch", "aspen"],
        ("mixed", "LV"): ["pine", "spruce", "birch"],
        ("mixed", "LT"): ["pine", "spruce", "oak", "birch"],
    }

    key = (forest_type, country_code)
    if key in species_map:
        return species_map[key]

    # Generic defaults
    defaults = {
        "coniferous": ["spruce", "pine"],
        "deciduous": ["birch", "oak", "beech"],
        "mixed": ["spruce", "pine", "birch"],
        "scrub": ["juniper", "willow"],
        "heath": ["heather"],
    }
    return defaults.get(forest_type, ["generic_tree"])


def extract_building_features(
    building_data: dict,
    country_code: str = "UNKNOWN",
    job = None,
) -> dict:
    """
    Process building footprints for Enfusion object placement.

    Maps building types to Enfusion prefabs and generates
    placement coordinates.

    Args:
        building_data: GeoJSON FeatureCollection of buildings
        country_code: For country-specific building style rules

    Returns:
        Processed building data with prefab mapping
    """
    if not building_data or not building_data.get("features"):
        return {"buildings": [], "stats": {}}

    buildings = []

    # Building type to Enfusion prefab category mapping
    prefab_categories = {
        "residential": "Building_Residential",
        "house": "Building_House",
        "apartments": "Building_Apartments",
        "detached": "Building_House",
        "church": "Building_Church",
        "chapel": "Building_Church",
        "commercial": "Building_Commercial",
        "industrial": "Building_Industrial",
        "warehouse": "Building_Industrial",
        "garage": "Building_Garage",
        "garages": "Building_Garage",
        "barn": "Building_Barn",
        "farm": "Building_Barn",
        "farm_auxiliary": "Building_Shed",
        "shed": "Building_Shed",
        "school": "Building_Commercial",
        "hospital": "Building_Commercial",
        "office": "Building_Commercial",
        "retail": "Building_Commercial",
        "yes": "Building_Generic",
    }

    for feature in building_data["features"]:
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})

        if geom.get("type") != "Polygon":
            continue

        building_type = props.get("building_type", "yes")
        height = props.get("height", 0)
        if not height:
            height_defaults = {
                "apartments": 15,
                "commercial": 12,
                "industrial": 8,
                "warehouse": 6,
                "church": 20,
                "residential": 6,
                "house": 6,
                "garage": 3,
                "shed": 3,
                "barn": 6,
            }
            height = height_defaults.get(building_type, 6)

        # Calculate centroid for placement
        try:
            import math
            from shapely.geometry import shape
            poly = shape(geom)
            centroid = poly.centroid
            center = [centroid.x, centroid.y]
            rotation = _estimate_building_rotation(geom)
            # Convert degree^2 to m^2 using centroid latitude
            m_per_deg_lon = 111320.0 * math.cos(math.radians(centroid.y))
            m_per_deg_lat = 110540.0
            footprint_area = poly.area * m_per_deg_lon * m_per_deg_lat
        except Exception:
            coords = geom.get("coordinates", [[]])[0]
            if coords:
                center = [
                    sum(c[0] for c in coords) / len(coords),
                    sum(c[1] for c in coords) / len(coords),
                ]
            else:
                continue
            rotation = 0
            footprint_area = 0

        prefab_category = prefab_categories.get(building_type, "Building_Generic")

        buildings.append({
            "osm_id": props.get("osm_id"),
            "name": props.get("name", ""),
            "building_type": building_type,
            "height_m": height,
            "center": center,
            "rotation_deg": rotation,
            "footprint_area_m2": footprint_area,
            "prefab_category": prefab_category,
            "geometry": geom,
        })

    stats = {
        "total": len(buildings),
        "by_type": {},
    }
    for b in buildings:
        t = b["building_type"]
        stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

    # Get top 5 building types
    top_types = dict(sorted(stats["by_type"].items(), key=lambda x: x[1], reverse=True)[:5])

    logger.info(
        f"Extracted {stats['total']} buildings. "
        f"Top types: {top_types}"
    )

    if job:
        type_details = ", ".join([f"{k}: {v}" for k, v in list(top_types.items())[:5]])
        job.add_log(
            f"✓ Building extraction: {stats['total']} structures ({type_details})",
            "success"
        )

    return {
        "buildings": buildings,
        "stats": stats,
    }


def _estimate_building_rotation(geom: dict) -> float:
    """Estimate building rotation from longest wall edge."""
    coords = geom.get("coordinates", [[]])[0]
    if len(coords) < 4:
        return 0

    max_len = 0
    best_angle = 0

    for i in range(len(coords) - 1):
        dx = coords[i + 1][0] - coords[i][0]
        dy = coords[i + 1][1] - coords[i][1]
        length = np.sqrt(dx**2 + dy**2)
        if length > max_len:
            max_len = length
            best_angle = np.degrees(np.arctan2(dy, dx))

    return best_angle % 360


def extract_all_features(osm_data: dict, country_code: str, job = None) -> dict:
    """
    Extract all features from OSM data.

    Args:
        osm_data: Dict with roads, water, forests, buildings, land_use
        country_code: ISO country code
        job: Optional MapGenerationJob for logging

    Returns:
        Dict with all extracted features
    """
    if job:
        job.add_log("Extracting water features...")
    water = extract_water_features(osm_data.get("water", {}), country_code, job)

    if job:
        job.add_log("Extracting forest features...")
    forests = extract_forest_features(osm_data.get("forests", {}), country_code, job)

    if job:
        job.add_log("Extracting building features...")
    buildings = extract_building_features(osm_data.get("buildings", {}), country_code, job)

    summary = {
        "lakes": water["stats"].get("lakes", 0),
        "rivers": water["stats"].get("rivers", 0),
        "forest_areas": forests["stats"].get("total_areas", 0),
        "buildings": buildings["stats"].get("total", 0),
    }

    if job:
        job.add_log(
            f"Extracted {summary['lakes']} lakes, {summary['rivers']} rivers, "
            f"{summary['forest_areas']} forests, {summary['buildings']} buildings",
            "success"
        )

    return {
        "water": water,
        "forests": forests,
        "buildings": buildings,
        "summary": summary,
    }
