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

from services.heightmap_generator import flatten_water_in_heightmap  # noqa: E402


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
