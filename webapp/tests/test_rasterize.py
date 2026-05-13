"""
Tests for services/utils/rasterize.py.

Focuses on the polygon-with-holes behaviour that was broken pre-v1.0.5
(every ring was filled, so islands inside lake polygons were rendered as
water just like the lake itself).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from services.utils.rasterize import (  # noqa: E402
    rasterize_features_to_mask,
    rasterize_lines_per_feature_width,
)


# A 1°×1° bbox with width/height = 100 px → 1 pixel ≈ 0.01° on each side.
BBOX = (0.0, 0.0, 1.0, 1.0)
W, H = 100, 100


def _polygon_feature(coords: list[list[list[float]]]) -> dict:
    """Build a single-feature GeoJSON FeatureCollection for a Polygon."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": coords},
                "properties": {},
            }
        ],
    }


def _multipolygon_feature(coords: list[list[list[list[float]]]]) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "MultiPolygon", "coordinates": coords},
                "properties": {},
            }
        ],
    }


class TestPolygonHoles:
    def test_polygon_without_holes_fills_interior(self):
        # Square covering the whole bbox
        square = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
        mask = rasterize_features_to_mask(_polygon_feature(square), W, H, BBOX)
        # Allow 1 pixel slack on each edge for half-open boundaries
        assert mask[H // 2, W // 2] == 1
        assert mask.sum() > 0.95 * W * H

    def test_polygon_with_hole_excludes_hole_interior(self):
        # Outer 1°×1° square with an inner 0.5°×0.5° square hole centered at (0.5, 0.5)
        outer = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
        hole = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75], [0.25, 0.25]]
        mask = rasterize_features_to_mask(_polygon_feature([outer, hole]), W, H, BBOX)

        # Pixel at the centre (in the hole) must be 0 (LAND, not water)
        assert mask[H // 2, W // 2] == 0, (
            "Centre pixel should be inside the hole and unmasked — "
            "this is the regression: pre-v1.0.5 it was incorrectly filled."
        )
        # Pixel near a corner (in the outer ring, outside the hole) must be 1
        assert mask[5, 5] == 1
        assert mask[H - 5, W - 5] == 1

    def test_multipolygon_holes_respected(self):
        # Two separate polygons, both with holes
        poly_a = [
            [[0.0, 0.0], [0.4, 0.0], [0.4, 0.4], [0.0, 0.4], [0.0, 0.0]],   # outer
            [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.3], [0.1, 0.1]],   # hole
        ]
        poly_b = [
            [[0.6, 0.6], [1.0, 0.6], [1.0, 1.0], [0.6, 1.0], [0.6, 0.6]],   # outer
            [[0.7, 0.7], [0.9, 0.7], [0.9, 0.9], [0.7, 0.9], [0.7, 0.7]],   # hole
        ]
        mask = rasterize_features_to_mask(_multipolygon_feature([poly_a, poly_b]), W, H, BBOX)

        # Poly A: ring centre should be filled, hole centre should be empty
        py, px = int(H * (1 - 0.05)), int(W * 0.05)  # near (0.05, 0.05) in geo
        assert mask[py, px] == 1
        py, px = int(H * (1 - 0.2)), int(W * 0.2)  # near (0.2, 0.2) — inside hole
        assert mask[py, px] == 0

        # Poly B: same pattern
        py, px = int(H * (1 - 0.65)), int(W * 0.65)
        assert mask[py, px] == 1
        py, px = int(H * (1 - 0.8)), int(W * 0.8)  # inside hole
        assert mask[py, px] == 0

        # Gap between A and B should be empty
        py, px = int(H * (1 - 0.5)), int(W * 0.5)
        assert mask[py, px] == 0

    def test_holes_in_one_polygon_dont_erase_other_polygons(self):
        # Polygon A has a hole. Polygon B's exterior sits inside that hole.
        # Pre-v1.0.5 either (a) hole was ignored (filled = bug), or
        # (b) a single-image hole-fill would erase B if A came after B.
        poly_a = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]],
                    [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]],
                ],
            },
            "properties": {},
        }
        poly_b = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]],
                ],
            },
            "properties": {},
        }
        # Order: B first, then A. A's hole punch must NOT erase B.
        gj = {"type": "FeatureCollection", "features": [poly_b, poly_a]}
        mask = rasterize_features_to_mask(gj, W, H, BBOX)

        # Centre of the map is inside B → must be 1
        assert mask[H // 2, W // 2] == 1, (
            "Polygon B's interior must survive Polygon A's hole punch — "
            "compositing must be per-feature."
        )
        # Inside A's exterior but outside the hole and outside B → 1
        assert mask[10, 10] == 1
        # Inside A's hole and outside B → 0
        py, px = int(H * (1 - 0.25)), int(W * 0.25)
        assert mask[py, px] == 0


class TestEmptyAndDegenerate:
    def test_empty_features_returns_zero_mask(self):
        mask = rasterize_features_to_mask({"features": []}, W, H, BBOX)
        assert mask.shape == (H, W)
        assert mask.sum() == 0

    def test_degenerate_bbox_returns_zero_mask(self):
        square = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
        mask = rasterize_features_to_mask(
            _polygon_feature(square), W, H, (1.0, 1.0, 1.0, 1.0)
        )
        assert mask.sum() == 0


class TestLines:
    def test_linestring_is_drawn(self):
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.0, 0.5], [1.0, 0.5]],
                    },
                    "properties": {},
                }
            ],
        }
        mask = rasterize_features_to_mask(gj, W, H, BBOX, buffer_px=1)
        # The horizontal centre row should have at least one painted pixel
        assert mask[H // 2].sum() > 0

    def test_per_feature_width_renders_wider_for_wider_roads(self):
        # Two horizontal LineStrings; one labelled width=4, the other width=14.
        # The buffer_px_fn translates each to a different pixel half-width and
        # the resulting mask must have a thicker stripe for the wider road.
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.0, 0.25], [1.0, 0.25]],
                    },
                    "properties": {"width_m": 4},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.0, 0.75], [1.0, 0.75]],
                    },
                    "properties": {"width_m": 14},
                },
            ],
        }
        # Width 14 → half=7 → pixel width 15; width 4 → half=2 → pixel width 5.
        buffer_fn = lambda f: f["properties"]["width_m"] // 2
        mask = rasterize_lines_per_feature_width(
            gj, W, H, BBOX, buffer_px_fn=buffer_fn,
        )

        # Find the painted-stripe height around each road.
        # narrow stripe: roughly centered at y = H * (1 - 0.25) = 75
        # wide stripe:   roughly centered at y = H * (1 - 0.75) = 25
        narrow_col = mask[:, W // 2]
        narrow_stripe_y = np.where(narrow_col > 0)[0]
        wide_col = mask[:, W // 2]
        # Both stripes share the same column; split by centre y
        narrow_band = narrow_stripe_y[narrow_stripe_y > H // 2]
        wide_band = narrow_stripe_y[narrow_stripe_y < H // 2]

        assert len(narrow_band) >= 1
        assert len(wide_band) >= 1
        # Wider road must paint strictly more pixels per column.
        assert len(wide_band) > len(narrow_band), (
            f"Wider road must produce a thicker stripe; got narrow={len(narrow_band)}, "
            f"wide={len(wide_band)} — per-feature buffer_px_fn likely ignored."
        )

    def test_per_feature_filter_excludes_unwanted_features(self):
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.0, 0.5], [1.0, 0.5]],
                    },
                    "properties": {"surface": "asphalt"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.5, 0.0], [0.5, 1.0]],
                    },
                    "properties": {"surface": "gravel"},
                },
            ],
        }
        mask = rasterize_lines_per_feature_width(
            gj, W, H, BBOX,
            buffer_px_fn=lambda f: 1,
            filter_fn=lambda f: f["properties"]["surface"] == "asphalt",
        )
        # Horizontal asphalt line is drawn (centre row has pixels).
        assert mask[H // 2].sum() > 0
        # Vertical gravel line is filtered out (centre column has only the
        # one painted pixel from the asphalt row).
        col_pixels = mask[:, W // 2].sum()
        assert col_pixels <= 3, (
            f"Filtered-out gravel line should not be drawn; column sum={col_pixels}"
        )

    def test_polygon_holes_dont_erase_lines(self):
        # Polygon with hole AND a line crossing the hole
        gj = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]],
                            [[0.3, 0.3], [0.7, 0.3], [0.7, 0.7], [0.3, 0.7], [0.3, 0.3]],
                        ],
                    },
                    "properties": {},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0.0, 0.5], [1.0, 0.5]],
                    },
                    "properties": {},
                },
            ],
        }
        mask = rasterize_features_to_mask(gj, W, H, BBOX, buffer_px=1)
        # The line crosses the hole at the centre — it must still be drawn
        assert mask[H // 2, W // 2] == 1
