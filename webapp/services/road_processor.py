"""
Road processing service.

Processes OSM road data into Arma Reforger spline data:
- Classifies roads by type (motorway, gravel, dirt, etc.)
- Infers surface type and width from OSM tags + country rules
- Generates spline control points
- Maps to Enfusion road generator prefabs
"""

import logging
from typing import Optional

import numpy as np

from config import ROAD_DEFAULT_SURFACE, ROAD_DEFAULT_WIDTH, ROAD_ENFUSION_PREFAB

logger = logging.getLogger(__name__)


# Country-specific road surface inference rules (fallback from config)
DEFAULT_ROAD_RULES = {
    "track_surface_default": "gravel",
    "residential_rural_surface": "asphalt",
    "forest_road_surface": "gravel",
}

# Enfusion road prefab mapping by (surface, width_class)
ENFUSION_ROAD_PREFABS = {
    ("asphalt", "wide"): "RG_Road_Asphalt_8m",
    ("asphalt", "medium"): "RG_Road_Asphalt_6m",
    ("asphalt", "narrow"): "RG_Road_Asphalt_4m",
    ("gravel", "wide"): "RG_Road_Gravel_6m",
    ("gravel", "medium"): "RG_Road_Gravel_4m",
    ("gravel", "narrow"): "RG_Road_Gravel_3m",
    ("dirt", "wide"): "RG_Road_Dirt_4m",
    ("dirt", "medium"): "RG_Road_Dirt_3m",
    ("dirt", "narrow"): "RG_Road_Dirt_2m",
}


def infer_road_surface(
    highway_type: str,
    osm_surface: str,
    country_code: str,
    is_in_forest: bool = False,
    is_in_urban: bool = False,
) -> str:
    """
    Infer road surface material from OSM tags and context.

    Priority:
    1. Explicit OSM surface tag
    2. Country-specific rules based on highway type and context
    3. Default rules
    """
    # If OSM has explicit surface tag, use it
    if osm_surface:
        surface_map = {
            "asphalt": "asphalt",
            "paved": "asphalt",
            "concrete": "asphalt",
            "concrete:plates": "asphalt",
            "concrete:lanes": "asphalt",
            "sett": "asphalt",
            "cobblestone": "asphalt",
            "paving_stones": "asphalt",
            "gravel": "gravel",
            "fine_gravel": "gravel",
            "compacted": "gravel",
            "dirt": "dirt",
            "earth": "dirt",
            "ground": "dirt",
            "mud": "dirt",
            "sand": "dirt",
            "grass": "dirt",
            "unpaved": "gravel",
        }
        return surface_map.get(osm_surface, "gravel")

    rules = ROAD_DEFAULT_SURFACE.get(country_code, DEFAULT_ROAD_RULES)

    # Infer from highway type and context
    if highway_type in ("motorway", "motorway_link", "trunk", "trunk_link",
                         "primary", "primary_link", "secondary", "secondary_link"):
        return "asphalt"

    if highway_type in ("tertiary", "tertiary_link"):
        return "asphalt"

    if highway_type == "residential":
        if is_in_urban:
            return "asphalt"
        return rules.get("residential_rural_surface", "asphalt")

    if highway_type == "unclassified":
        return "asphalt" if is_in_urban else "gravel"

    if highway_type == "service":
        return "asphalt" if is_in_urban else "gravel"

    if highway_type == "track":
        if is_in_forest:
            return rules.get("forest_road_surface", "gravel")
        return rules.get("track_surface_default", "gravel")

    if highway_type in ("path", "footway", "bridleway"):
        return "dirt"

    if highway_type == "cycleway":
        return "asphalt" if is_in_urban else "gravel"

    return "gravel"


def infer_road_width(
    highway_type: str,
    osm_width: str,
    osm_lanes: str,
    surface: str,
) -> float:
    """Infer road width in meters."""
    # Use explicit width if available
    if osm_width:
        try:
            return float(osm_width.replace("m", "").strip())
        except ValueError:
            pass

    # Infer from lanes
    if osm_lanes:
        try:
            lanes = int(osm_lanes)
            lane_width = 3.5 if surface == "asphalt" else 2.5
            return lanes * lane_width
        except ValueError:
            pass

    # Default widths from config
    road_info = ROAD_DEFAULT_WIDTH.get(highway_type)
    if road_info:
        return road_info["width"]

    return 4.0


def get_width_class(width: float) -> str:
    """Classify road width for prefab selection."""
    if width >= 7:
        return "wide"
    elif width >= 4:
        return "medium"
    else:
        return "narrow"


def process_roads(
    road_features: dict,
    country_code: str,
    terrain_origin: tuple = (0, 0),
    terrain_bounds: Optional[dict] = None,
    job = None,
) -> dict:
    """
    Process OSM road features into Enfusion-ready spline data.

    Args:
        road_features: GeoJSON FeatureCollection of roads
        country_code: ISO country code for country-specific rules
        terrain_origin: (x, y) origin offset for terrain coordinates
        terrain_bounds: Bounding box for coordinate transformation
        job: Optional MapGenerationJob for logging

    Returns:
        Dict with processed road data including spline points and prefab mapping
    """
    if not road_features or not road_features.get("features"):
        return {"roads": [], "stats": {}}

    if job:
        job.add_log(f"Processing {len(road_features['features'])} road segments...")

    processed = []
    stats = {
        "total": 0,
        "by_surface": {},
        "by_type": {},
    }

    for feature in road_features["features"]:
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})

        if geom.get("type") != "LineString":
            continue

        coords = geom.get("coordinates", [])
        if len(coords) < 2:
            continue

        highway_type = props.get("highway", "unclassified")
        osm_surface = props.get("surface", "")
        osm_width = props.get("width", "")
        osm_lanes = props.get("lanes", "")
        is_bridge = props.get("bridge", "no") == "yes"
        is_tunnel = props.get("tunnel", "no") == "yes"

        # Infer surface and width
        surface = infer_road_surface(
            highway_type, osm_surface, country_code,
            is_in_forest=False,
            is_in_urban=highway_type in ("residential", "service", "living_street"),
        )
        width = infer_road_width(highway_type, osm_width, osm_lanes, surface)
        width_class = get_width_class(width)

        # Get Enfusion prefab (try config first, then local mapping)
        prefab = ROAD_ENFUSION_PREFAB.get(highway_type)
        if not prefab:
            prefab = ENFUSION_ROAD_PREFABS.get(
                (surface, width_class),
                f"RG_Road_{surface.capitalize()}_{int(width)}m"
            )

        # Convert coordinates to spline control points
        spline_points = []
        for lng, lat in coords:
            spline_points.append({
                "x": lng,
                "y": lat,
                "z": 0,  # Elevation will be set from heightmap
            })

        road_data = {
            "osm_id": props.get("osm_id"),
            "name": props.get("name", ""),
            "highway_type": highway_type,
            "surface": surface,
            "width_m": width,
            "is_bridge": is_bridge,
            "is_tunnel": is_tunnel,
            "enfusion_prefab": prefab,
            "spline_points": spline_points,
            "point_count": len(spline_points),
        }
        processed.append(road_data)

        stats["total"] += 1
        stats["by_surface"][surface] = stats["by_surface"].get(surface, 0) + 1
        stats["by_type"][highway_type] = stats["by_type"].get(highway_type, 0) + 1

    # Get top 5 road types
    top_types = dict(sorted(stats["by_type"].items(), key=lambda x: x[1], reverse=True)[:5])

    logger.info(
        f"Processed {stats['total']} road segments. "
        f"By surface: {stats['by_surface']}. "
        f"Top types: {top_types}"
    )

    if job:
        # Format detailed breakdown for activity log
        surface_details = ", ".join([f"{k}: {v}" for k, v in stats['by_surface'].items()])
        type_details = ", ".join([f"{k}: {v}" for k, v in list(top_types.items())[:5]])
        job.add_log(
            f"✓ Classified {stats['total']} roads by surface ({surface_details}) "
            f"and type ({type_details})",
            "success"
        )

    return {
        "roads": processed,
        "stats": stats,
    }


def export_roads_geojson(processed_roads: dict) -> dict:
    """
    Export processed roads as GeoJSON with Enfusion metadata.
    """
    features = []
    for road in processed_roads.get("roads", []):
        coords = [[p["x"], p["y"]] for p in road["spline_points"]]
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "osm_id": road["osm_id"],
                "name": road["name"],
                "highway_type": road["highway_type"],
                "surface": road["surface"],
                "width_m": road["width_m"],
                "is_bridge": road["is_bridge"],
                "is_tunnel": road["is_tunnel"],
                "enfusion_prefab": road["enfusion_prefab"],
            },
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def export_roads_spline_csv(processed_roads: dict, transformer=None) -> str:
    """
    Export road spline data as CSV for potential scripted import.

    If a CoordinateTransformer is provided, coordinates are output in
    Enfusion local metres. Otherwise, WGS84 coordinates are used.

    Args:
        processed_roads: Processed road data dict.
        transformer: Optional CoordinateTransformer for local coordinates.

    Returns:
        CSV string with road spline data.
    """
    if transformer:
        lines = ["road_id,prefab,name,surface,width_m,point_index,local_x,local_z,elevation"]
        for i, road in enumerate(processed_roads.get("roads", [])):
            for j, point in enumerate(road["spline_points"]):
                local_x, local_z = transformer.wgs84_to_local(point["x"], point["y"])
                lines.append(
                    f"{road['osm_id']},{road['enfusion_prefab']},"
                    f"\"{road['name']}\",{road['surface']},{road['width_m']},"
                    f"{j},{local_x:.3f},{local_z:.3f},{point['z']:.2f}"
                )
    else:
        lines = ["road_id,prefab,name,surface,width_m,point_index,longitude,latitude,elevation"]
        for i, road in enumerate(processed_roads.get("roads", [])):
            for j, point in enumerate(road["spline_points"]):
                lines.append(
                    f"{road['osm_id']},{road['enfusion_prefab']},"
                    f"\"{road['name']}\",{road['surface']},{road['width_m']},"
                    f"{j},{point['x']:.8f},{point['y']:.8f},{point['z']:.2f}"
                )
    return "\n".join(lines)


def export_roads_geojson_local(processed_roads: dict, transformer) -> dict:
    """
    Export processed roads as GeoJSON with Enfusion local metre coordinates.

    Args:
        processed_roads: Processed road data dict.
        transformer: CoordinateTransformer for WGS84 -> local conversion.

    Returns:
        GeoJSON FeatureCollection with local coordinates.
    """
    features = []
    for road in processed_roads.get("roads", []):
        coords = []
        for p in road["spline_points"]:
            local_x, local_z = transformer.wgs84_to_local(p["x"], p["y"])
            coords.append([round(local_x, 3), round(local_z, 3)])

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "osm_id": road["osm_id"],
                "name": road["name"],
                "highway_type": road["highway_type"],
                "surface": road["surface"],
                "width_m": road["width_m"],
                "is_bridge": road["is_bridge"],
                "is_tunnel": road["is_tunnel"],
                "enfusion_prefab": road["enfusion_prefab"],
                "coordinate_system": "enfusion_local_metres",
            },
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
    }
