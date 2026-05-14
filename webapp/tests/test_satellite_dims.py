"""
Tests for the satellite texture dimension helper (issue #67 — blurry imagery).

The satellite texture is rendered at a higher resolution than the heightmap
so we don't throw away source detail. These tests pin the multiplier and the
SATELLITE_MAX_DIM cap. Pure Python — no rasterio/pyproj required, so they
run on every dev environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from services.satellite_service import (  # noqa: E402
    SATELLITE_MAX_DIM,
    SATELLITE_RESOLUTION_MULTIPLIER,
    compute_satellite_target_dims,
)


class TestComputeSatelliteTargetDims:
    def test_default_2049_heightmap_scales_up_capped_at_max(self):
        """A 2049 heightmap × multiplier=4 = 8196 → capped to 8192."""
        x, z = compute_satellite_target_dims(2049, 2049)
        assert x == SATELLITE_MAX_DIM
        assert z == SATELLITE_MAX_DIM

    def test_small_heightmap_scales_up_uncapped(self):
        """1025 × 4 = 4100, well below the 8192 cap, so no clamping."""
        x, z = compute_satellite_target_dims(1025, 1025)
        assert x == 1025 * SATELLITE_RESOLUTION_MULTIPLIER
        assert z == 1025 * SATELLITE_RESOLUTION_MULTIPLIER

    def test_rectangular_axes_capped_independently(self):
        """Non-square heightmap: the larger axis caps first, the smaller one
        scales freely."""
        x, z = compute_satellite_target_dims(2049, 513)
        # x = min(8192, 8196) = 8192
        # z = min(8192, 2052) = 2052
        assert x == SATELLITE_MAX_DIM
        assert z == 513 * SATELLITE_RESOLUTION_MULTIPLIER

    def test_satellite_strictly_larger_than_heightmap(self):
        """For every valid Enfusion heightmap size, the satellite dim must be
        strictly larger (or equal at the cap) — never smaller."""
        for size in (129, 257, 513, 1025, 2049, 4097, 8193):
            x, _ = compute_satellite_target_dims(size, size)
            assert x >= size, (
                f"satellite dim {x} smaller than heightmap dim {size}"
            )
