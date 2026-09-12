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

from typing import Callable

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
            line_width = max(1, buffer_px * 2) if buffer_px > 0 else 1
            _draw_clipped_line(
                line_draw, coords, west, north, lng_range, lat_range,
                width, height, line_width,
            )

        elif geom_type == "MultiLineString" and coords:
            line_width = max(1, buffer_px * 2) if buffer_px > 0 else 1
            for line_coords in coords:
                _draw_clipped_line(
                    line_draw, line_coords, west, north, lng_range, lat_range,
                    width, height, line_width,
                )

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


def rasterize_lines_per_feature_width(
    geojson: dict,
    width: int,
    height: int,
    bbox_wgs84: tuple[float, float, float, float],
    buffer_px_fn: Callable[[dict], int],
    filter_fn: Callable[[dict], bool] | None = None,
) -> np.ndarray:
    """
    Rasterize LineString / MultiLineString features with a per-feature buffer.

    Unlike rasterize_features_to_mask which applies one global buffer to every
    line, this variant calls `buffer_px_fn(feature)` for each feature so that
    e.g. a 4m residential street and a 14m motorway are rendered at different
    pixel widths — which lets the asphalt surface mask match the per-feature
    widths used to generate the road splines.

    Args:
        geojson: GeoJSON FeatureCollection.
        width:   Output raster width in pixels.
        height:  Output raster height in pixels.
        bbox_wgs84: (west, south, east, north) bounding box.
        buffer_px_fn: Callable receiving a feature dict and returning the
            per-feature half-width in pixels. Drawn line width = 2*half+1.
        filter_fn: Optional callable receiving a feature dict and returning
            True if the feature should be drawn. Use this to reproduce the
            road-surface classification logic (so the mask matches the spline).

    Returns:
        Binary uint8 mask (0 or 1).
    """
    west, south, east, north = bbox_wgs84
    lng_range = east - west
    lat_range = north - south
    if lng_range <= 0 or lat_range <= 0:
        return np.zeros((height, width), dtype=np.uint8)

    line_img = Image.new("L", (width, height), 0)
    line_draw = ImageDraw.Draw(line_img)

    for feature in geojson.get("features", []):
        if filter_fn is not None and not filter_fn(feature):
            continue

        geom = feature.get("geometry", {})
        geom_type = geom.get("type", "")
        coords = geom.get("coordinates", [])
        if not coords:
            continue

        half = max(0, int(buffer_px_fn(feature)))
        line_width = max(1, half * 2 + 1)

        if geom_type == "LineString":
            _draw_clipped_line(
                line_draw, coords, west, north, lng_range, lat_range,
                width, height, line_width,
            )
        elif geom_type == "MultiLineString":
            for line_coords in coords:
                _draw_clipped_line(
                    line_draw, line_coords, west, north, lng_range, lat_range,
                    width, height, line_width,
                )

    return (np.array(line_img) > 0).astype(np.uint8)


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


def _coords_to_pixels_unclamped(
    coords: list,
    west: float,
    north: float,
    lng_range: float,
    lat_range: float,
    width: int,
    height: int,
) -> list[tuple[float, float]]:
    """Geographic coordinates to *unclamped* float pixel coordinates.

    Companion to ``_coords_to_pixels``; keeps vertices outside the raster where
    they actually are, so ``_clip_polyline`` can cut the line at the map border
    rather than folding it onto the border. See ``_clip_polyline``.
    """
    pixels = []
    for coord in coords:
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            lng, lat = float(coord[0]), float(coord[1])
            pixels.append((
                (lng - west) / lng_range * width,
                (north - lat) / lat_range * height,
            ))
    return pixels


def _clip_segment(
    x0: float, y0: float, x1: float, y1: float,
    xmax: float, ymax: float,
) -> tuple[float, float, float, float] | None:
    """Liang-Barsky clip of one segment to the box [0, xmax] x [0, ymax].

    Returns the clipped endpoints, or None when the segment misses the box.
    """
    dx = x1 - x0
    dy = y1 - y0
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, x0), (dx, xmax - x0), (-dy, y0), (dy, ymax - y0)):
        if pp == 0:
            if qq < 0:
                return None          # parallel to this edge and outside it
            continue
        t = qq / pp
        if pp < 0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _clip_polyline(
    pixels: list[tuple[float, float]],
    width: int,
    height: int,
) -> list[list[tuple[int, int]]]:
    """Split a polyline into the pieces that lie inside the raster.

    ``_coords_to_pixels`` **clamps** out-of-range vertices onto the raster
    edge. For a filled polygon that is harmless, but for a line it is not: a
    road that leaves the map and runs two kilometres past it has every outside
    vertex collapsed onto the border, and ImageDraw then joins them into a
    solid road drawn *along* the map edge. On a generated Swedish map that
    produced a single contiguous 1147 px (2.3 km) run of full-value gravel
    across the top edge of ``surface_gravel.png``, and similar rings on the
    other three edges of every map (found while verifying the #202 fix).

    Clipping instead of clamping drops the outside portion entirely and keeps
    the crossing point exact, so a road that merely passes through the corner
    of the map paints only the part that is really there.

    Returns a list of polylines, each with >= 2 integer points.
    """
    if len(pixels) < 2:
        return []
    xmax, ymax = float(width - 1), float(height - 1)
    runs: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    for (x0, y0), (x1, y1) in zip(pixels, pixels[1:]):
        clipped = _clip_segment(x0, y0, x1, y1, xmax, ymax)
        if clipped is None:
            if len(current) >= 2:
                runs.append(current)
            current = []
            continue
        cx0, cy0, cx1, cy1 = clipped
        a = (int(round(cx0)), int(round(cy0)))
        b = (int(round(cx1)), int(round(cy1)))
        if not current:
            current = [a, b]
        elif current[-1] == a:
            current.append(b)
        else:
            # The previous segment was cut short: this one re-enters elsewhere.
            if len(current) >= 2:
                runs.append(current)
            current = [a, b]
    if len(current) >= 2:
        runs.append(current)
    return runs


def _draw_clipped_line(
    draw,
    coords: list,
    west: float,
    north: float,
    lng_range: float,
    lat_range: float,
    width: int,
    height: int,
    line_width: int,
) -> None:
    """Project, clip to the raster, and draw a LineString's inside portions."""
    pixels = _coords_to_pixels_unclamped(
        coords, west, north, lng_range, lat_range, width, height
    )
    for run in _clip_polyline(pixels, width, height):
        draw.line(run, fill=255, width=line_width)


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
