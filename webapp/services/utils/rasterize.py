"""
Fast GeoJSON-to-raster utilities using PIL.ImageDraw.

Replaces the O(n^2) per-pixel shapely.Point.contains() approach with
vectorized polygon/line drawing, which is orders of magnitude faster.

Used by both heightmap_generator and surface_mask_generator.

Polygon-with-holes handling:
    GeoJSON Polygons store the exterior ring as coordinates[0] and interior
    rings (holes) as coordinates[1..]. PIL's ImageDraw.polygon doesn't
    natively support holes, so we render each polygon feature into its own
    temporary image (exterior=255, holes=0) and OR-composite the result
    into the shared mask. That way a hole in one feature can never erase
    pixels owned by a different feature — critical for cases like Lake
    Storsjön (one polygon with island holes) overlapping a smaller pond
    polygon that happens to sit inside an island.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageChops, ImageDraw
from scipy import ndimage


def rasterize_features_to_mask(
    geojson: dict,
    width: int,
    height: int,
    bbox_wgs84: tuple[float, float, float, float],
    filter_tags: dict[str, list[str]] | None = None,
    buffer_px: int = 0,
) -> np.ndarray:
    """
    Fast rasterization of GeoJSON features to a binary mask using PIL.ImageDraw.

    Args:
        geojson: GeoJSON FeatureCollection
        width: Output raster width in pixels
        height: Output raster height in pixels
        bbox_wgs84: (west, south, east, north) bounding box
        filter_tags: Optional {tag_key: [allowed_values]} to filter features
        buffer_px: For lines, the half-width in pixels; for polygons,
                   morphological dilation iterations after drawing.

    Returns:
        Binary uint8 mask (0 or 1).
    """
    west, south, east, north = bbox_wgs84
    lng_range = east - west
    lat_range = north - south
    if lng_range <= 0 or lat_range <= 0:
        return np.zeros((height, width), dtype=np.uint8)

    # Polygons and lines accumulate into separate images so polygon holes
    # never erase line pixels, then are merged at the end.
    polygon_img = Image.new("L", (width, height), 0)
    line_img = Image.new("L", (width, height), 0)
    line_draw = ImageDraw.Draw(line_img)

    has_polygons = False

    for feature in geojson.get("features", []):
        # Apply tag filter
        if filter_tags:
            props = feature.get("properties", {})
            match = False
            for tag_key, allowed_values in filter_tags.items():
                val = props.get(tag_key, "")
                if val in allowed_values:
                    match = True
                    break
            if not match:
                continue

        geom = feature.get("geometry", {})
        geom_type = geom.get("type", "")
        coords = geom.get("coordinates", [])

        if geom_type == "Polygon" and coords:
            has_polygons = True
            polygon_img = _composite_polygon_with_holes(
                polygon_img, coords,
                west, north, lng_range, lat_range, width, height,
            )

        elif geom_type == "MultiPolygon" and coords:
            has_polygons = True
            for polygon_rings in coords:
                polygon_img = _composite_polygon_with_holes(
                    polygon_img, polygon_rings,
                    west, north, lng_range, lat_range, width, height,
                )

        elif geom_type == "LineString" and coords:
            pixels = _coords_to_pixels(coords, west, north, lng_range, lat_range, width, height)
            if len(pixels) >= 2:
                line_width = max(1, buffer_px * 2) if buffer_px > 0 else 1
                line_draw.line(pixels, fill=255, width=line_width)

        elif geom_type == "MultiLineString" and coords:
            for line_coords in coords:
                pixels = _coords_to_pixels(line_coords, west, north, lng_range, lat_range, width, height)
                if len(pixels) >= 2:
                    line_width = max(1, buffer_px * 2) if buffer_px > 0 else 1
                    line_draw.line(pixels, fill=255, width=line_width)

    # Merge polygon and line layers (per-pixel max)
    mask = np.array(ImageChops.lighter(polygon_img, line_img))

    # Apply morphological dilation for polygon buffer
    if buffer_px > 0 and has_polygons:
        struct = ndimage.generate_binary_structure(2, 1)
        mask = ndimage.binary_dilation(
            mask.astype(bool), struct, iterations=buffer_px
        ).astype(np.uint8) * 255

    # Convert 0/255 to 0/1
    return (mask > 0).astype(np.uint8)


def _composite_polygon_with_holes(
    accumulator: Image.Image,
    rings: list,
    west: float,
    north: float,
    lng_range: float,
    lat_range: float,
    width: int,
    height: int,
) -> Image.Image:
    """
    Render one Polygon (list of rings) and OR-composite it into the accumulator.

    `rings[0]` is the exterior (drawn with fill=255), `rings[1..]` are interior
    holes (drawn with fill=0). The temporary image is then merged via
    ImageChops.lighter so the holes only mask this polygon's own exterior —
    they cannot erase pixels contributed by previously-drawn polygons.
    """
    if not rings:
        return accumulator
    feature_img = Image.new("L", (width, height), 0)
    feature_draw = ImageDraw.Draw(feature_img)
    for i, ring in enumerate(rings):
        pixels = _coords_to_pixels(ring, west, north, lng_range, lat_range, width, height)
        if len(pixels) >= 3:
            fill = 255 if i == 0 else 0
            feature_draw.polygon(pixels, fill=fill)
    return ImageChops.lighter(accumulator, feature_img)


def _coords_to_pixels(
    coords: list,
    west: float,
    north: float,
    lng_range: float,
    lat_range: float,
    width: int,
    height: int,
) -> list[tuple[int, int]]:
    """Convert geographic coordinates to pixel coordinates."""
    pixels = []
    for coord in coords:
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            lng, lat = float(coord[0]), float(coord[1])
            px = int((lng - west) / lng_range * width)
            py = int((north - lat) / lat_range * height)
            px = max(0, min(width - 1, px))
            py = max(0, min(height - 1, py))
            pixels.append((px, py))
    return pixels
