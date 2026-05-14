"""
Tests for flatten_water_in_heightmap — shore-distance bathymetry (issue #66).

Pre-v1.3.4 the function set every pixel in a water region to a single water
level, producing a walkable seabed under the Lake Generator prefab. The new
behaviour carves a depth bowl below the water surface using the distance
transform from the shore.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

# scipy is a hard dep of heightmap_generator; skip the module if it's missing
pytest.importorskip("scipy", reason="scipy is required for heightmap tests")

from services.heightmap_generator import (  # noqa: E402
    _rasterize_river_mask,
    _synthesize_sea_mask,
    flatten_water_in_heightmap,
)


def _flat_terrain(size: int, elev: float = 100.0) -> np.ndarray:
    return np.full((size, size), elev, dtype=np.float32)


def _disk_mask(size: int, radius: int) -> np.ndarray:
    cy = cx = size // 2
    yy, xx = np.ogrid[:size, :size]
    return (((yy - cy) ** 2 + (xx - cx) ** 2) <= radius * radius).astype(np.uint8)


class TestBathymetry:
    def test_empty_mask_returns_input_unchanged(self):
        elevation = _flat_terrain(32)
        out = flatten_water_in_heightmap(
            elevation, np.zeros_like(elevation, dtype=np.uint8)
        )
        assert np.array_equal(out, elevation)

    def test_shore_pixels_stay_at_water_surface(self):
        """Pixels on the immediate inside edge of the water polygon must be
        exactly at water-surface level (depth = 0). This is what keeps the
        join with surrounding land smooth."""
        size = 64
        elevation = _flat_terrain(size, elev=100.0)
        mask = _disk_mask(size, radius=20)

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=0,  # disable shore blending for a clean assertion
            pixel_size_m=2.0,
            max_depth_m=10.0,
            shore_slope_m_per_m=0.5,
        )

        # The water_surface for this region is the 10th percentile of a flat
        # field, i.e. 100.0. Shore-ring pixels have an EDT distance between 1
        # and sqrt(2) px (diagonal neighbours), so depth is at most
        # sqrt(2) * 2 * 0.5 ≈ 1.41 m below the surface.
        ring = mask.astype(bool) & ~_disk_mask(size, radius=19).astype(bool)
        assert out[ring].max() <= 100.0
        assert out[ring].min() >= 100.0 - (2.0 ** 0.5) * 2.0 * 0.5 - 1e-3

    def test_deep_interior_reaches_max_depth(self):
        """A lake big enough that the centre is well past `max_depth / slope`
        metres from the shore must reach exactly `max_depth_m` below the
        water surface."""
        size = 200
        elevation = _flat_terrain(size, elev=50.0)
        mask = _disk_mask(size, radius=80)

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=0,
            pixel_size_m=2.0,
            max_depth_m=8.0,
            shore_slope_m_per_m=0.5,
        )

        center = out[size // 2, size // 2]
        # water_surface = 50, max depth = 8
        assert center == pytest.approx(50.0 - 8.0, abs=1e-5)

    def test_small_pond_stays_shallow(self):
        """A small pond (radius < max_depth / slope) must not hit max depth."""
        size = 32
        elevation = _flat_terrain(size, elev=10.0)
        mask = _disk_mask(size, radius=3)  # 3 px ≈ 6 m radius at 2 m/px

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=0,
            pixel_size_m=2.0,
            max_depth_m=10.0,
            shore_slope_m_per_m=0.5,
        )

        # Deepest pixel is at the centre, ~3 px from shore = 6 m → 3 m depth
        center = out[size // 2, size // 2]
        assert 6.0 < center < 9.0  # well above (10 - 10) = 0 the old behaviour would give

    def test_multiple_regions_get_independent_water_levels(self):
        """Two disjoint lakes at different elevations must each get their own
        surface — not be co-flattened."""
        size = 100
        elevation = np.full((size, size), 50.0, dtype=np.float32)
        elevation[:size, : size // 2] = 20.0  # left half lower

        mask = np.zeros_like(elevation, dtype=np.uint8)
        # Left lake centred at (50, 25), right lake centred at (50, 75)
        yy, xx = np.ogrid[:size, :size]
        mask[(yy - 50) ** 2 + (xx - 25) ** 2 <= 100] = 1
        mask[(yy - 50) ** 2 + (xx - 75) ** 2 <= 100] = 1

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=0,
            pixel_size_m=2.0,
            max_depth_m=4.0,
            shore_slope_m_per_m=1.0,
        )

        left_surface = out[50, 25] + 4.0  # add max depth back
        right_surface = out[50, 75] + 4.0
        assert left_surface == pytest.approx(20.0, abs=1.0)
        assert right_surface == pytest.approx(50.0, abs=1.0)

    def test_large_lake_has_gradient_not_plateau(self):
        """A lake big enough that the old constant-slope formula would have
        flattened most of its interior into a uniform plateau must now show
        a continuous depth gradient. ≥50 % of the interior pixels must lie
        strictly between the floor and the water surface."""
        size = 200
        elevation = _flat_terrain(size, elev=100.0)
        mask = _disk_mask(size, radius=90)  # ~180-px diameter — old slope = 0.3 m/m
                                            #   would plateau anything >54 px wide

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=0,
            pixel_size_m=2.0,
            max_depth_m=8.0,
            shore_slope_m_per_m=0.3,
        )

        interior = out[mask.astype(bool)]
        surface = 100.0
        floor = surface - 8.0
        strictly_between = (interior > floor + 1e-3) & (interior < surface - 1e-3)
        fraction = float(np.mean(strictly_between))
        assert fraction >= 0.5, (
            f"only {fraction:.0%} of interior pixels lie strictly between "
            f"floor={floor} and surface={surface} — plateau is back"
        )

    def test_region_depth_map_dispatch(self):
        """`region_depth_map` overrides `max_depth_m` per labelled region."""
        from scipy import ndimage as _ndi

        size = 100
        elevation = _flat_terrain(size, elev=50.0)
        mask = np.zeros_like(elevation, dtype=np.uint8)
        yy, xx = np.ogrid[:size, :size]
        mask[(yy - 50) ** 2 + (xx - 25) ** 2 <= 100] = 1
        mask[(yy - 50) ** 2 + (xx - 75) ** 2 <= 100] = 1

        labeled, _ = _ndi.label(mask)
        left_id = int(labeled[50, 25])
        right_id = int(labeled[50, 75])
        region_depth_map = {left_id: 1.0, right_id: 8.0}

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=0,
            pixel_size_m=2.0,
            max_depth_m=8.0,
            shore_slope_m_per_m=10.0,  # huge slope → ceiling wins
            region_depth_map=region_depth_map,
        )

        left_center = out[50, 25]
        right_center = out[50, 75]
        assert 48.5 < left_center < 49.5, (
            f"left center {left_center} not within ~1 m of expected 49 "
            f"(surface 50, ceiling 1)"
        )
        assert 41.5 < right_center < 42.5, (
            f"right center {right_center} not within ~1 m of expected 42 "
            f"(surface 50, ceiling 8)"
        )

    def test_shore_blend_does_not_pull_land_down_into_bowl(self):
        """Land just outside the lake must not be dragged toward the bowl
        bottom — only toward the water surface."""
        size = 64
        elevation = _flat_terrain(size, elev=100.0)
        mask = _disk_mask(size, radius=20)

        out = flatten_water_in_heightmap(
            elevation,
            mask,
            transition_px=5,
            pixel_size_m=2.0,
            max_depth_m=10.0,
            shore_slope_m_per_m=0.5,
        )

        # Pick a pixel 2 px outside the water mask (inside transition zone)
        # at the same y as the centre.
        shore_outside = out[size // 2, size // 2 + 22]
        # Inside the bowl proper:
        bowl_center = out[size // 2, size // 2]
        # The transition zone pixel must be MUCH closer to water surface (100)
        # than to bowl bottom (~90). Specifically: it must be above the
        # midpoint of the [bowl_center, surface] range.
        surface = 100.0
        midpoint = (bowl_center + surface) / 2
        assert shore_outside > midpoint, (
            f"transition pixel {shore_outside:.2f} dragged below "
            f"midpoint {midpoint:.2f} — shore blend leaked the bowl"
        )


def _line_feature(coords, water_type):
    return {
        "type": "Feature",
        "properties": {"water_type": water_type},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


class TestRiverRasterization:
    def test_river_linestring_carves_a_band(self):
        """A horizontal river LineString must produce a band of pixels several
        rows tall (matching its OSM-derived 15m width at 2 m/px)."""
        bbox = (0.0, 0.0, 0.001, 0.001)  # ~111 m × 111 m tile (synthetic)
        w_lon, s_lat, e_lon, n_lat = bbox
        # A horizontal line across the bbox at mid-latitude
        coords = [
            [w_lon + 0.0001, (s_lat + n_lat) / 2],
            [e_lon - 0.0001, (s_lat + n_lat) / 2],
        ]
        features = {"type": "FeatureCollection", "features": [
            _line_feature(coords, "river"),
        ]}

        mask = _rasterize_river_mask(features, 64, 64, bbox, pixel_size_m=2.0)

        # River = 15 m / (2 × 2 m) ≈ 4 px half-width → 9 px line width
        rows_with_river = np.where(mask.sum(axis=1) > 0)[0]
        assert rows_with_river.size >= 5, (
            f"river carved only {rows_with_river.size} rows — expected ≥5"
        )
        # And rows on the edge of the bbox stay dry (river is mid-y only)
        assert mask[0, :].sum() == 0
        assert mask[-1, :].sum() == 0

    def test_non_river_feature_is_ignored(self):
        """A coastline LineString must not be rasterized as a river."""
        bbox = (0.0, 0.0, 0.001, 0.001)
        coords = [[0.0001, 0.0005], [0.0009, 0.0005]]
        features = {"type": "FeatureCollection", "features": [
            _line_feature(coords, "coastline"),
        ]}
        mask = _rasterize_river_mask(features, 64, 64, bbox, pixel_size_m=2.0)
        assert mask.sum() == 0


class TestSeaSynthesis:
    def test_sea_synthesis_inland_map_returns_empty(self):
        """No coastline features → empty mask even when low ground exists."""
        bbox = (0.0, 0.0, 0.001, 0.001)
        features = {"type": "FeatureCollection", "features": []}
        elevation = np.full((64, 64), -5.0, dtype=np.float32)  # all below sea
        mask = _synthesize_sea_mask(features, elevation, bbox, pixel_size_m=2.0)
        assert mask.sum() == 0

    def test_sea_synthesis_low_half_with_coastline(self):
        """Left half low + right half high + coastline down the middle →
        sea mask covers ≥90 % of the left half and 0 % of the right half."""
        size = 100
        bbox = (0.0, 0.0, 0.001, 0.001)
        elevation = np.zeros((size, size), dtype=np.float32)
        elevation[:, : size // 2] = -2.0       # left half: below sea level
        elevation[:, size // 2 :] = 20.0       # right half: well above

        # Coastline LineString runs vertically near the middle of the bbox
        coords = [
            [bbox[0] + (bbox[2] - bbox[0]) * 0.5, bbox[1] + 1e-6],
            [bbox[0] + (bbox[2] - bbox[0]) * 0.5, bbox[3] - 1e-6],
        ]
        features = {"type": "FeatureCollection", "features": [
            _line_feature(coords, "coastline"),
        ]}

        mask = _synthesize_sea_mask(features, elevation, bbox, pixel_size_m=2.0)

        left = mask[:, : size // 2]
        right = mask[:, size // 2 :]
        left_fraction = float(left.mean())
        right_fraction = float(right.mean())
        assert left_fraction >= 0.9, (
            f"sea mask only covers {left_fraction:.0%} of the low (left) half"
        )
        assert right_fraction == 0.0, (
            f"sea mask leaked into the high (right) half ({right_fraction:.0%})"
        )
