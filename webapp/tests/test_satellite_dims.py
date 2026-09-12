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

    def test_rectangular_below_the_cap_scales_both_axes_freely(self):
        """Non-square heightmap under the cap: both axes take the multiplier."""
        x, z = compute_satellite_target_dims(1025, 513)
        assert x == 1025 * SATELLITE_RESOLUTION_MULTIPLIER
        assert z == 513 * SATELLITE_RESOLUTION_MULTIPLIER

    def test_rectangular_above_the_cap_preserves_aspect_ratio(self):
        """The satellite is stretched over the terrain, so its aspect ratio
        must match the heightmap's. Capping each axis independently at the
        same value squashed the imagery on non-square terrain (issue #197):
        a 4097x2049 heightmap used to produce an 8192x8192 texture."""
        x, z = compute_satellite_target_dims(4097, 2049)
        assert max(x, z) == SATELLITE_MAX_DIM, "longest axis must land on the cap"
        assert abs((x / z) - (4097 / 2049)) < 0.01, (
            f"satellite {x}x{z} does not match heightmap aspect 4097x2049"
        )

    def test_tall_rectangle_preserves_aspect_ratio(self):
        """Same rule with Z as the longer axis — no hidden X bias."""
        x, z = compute_satellite_target_dims(2049, 4097)
        assert max(x, z) == SATELLITE_MAX_DIM
        assert abs((z / x) - (4097 / 2049)) < 0.01

    def test_cap_is_never_exceeded(self):
        """SATELLITE_MAX_DIM is an engine texture limit, so it wins over the
        "never smaller than the heightmap" preference. The old max() floor
        overrode it: at MAX_TERRAIN_GRID_SIZE=16384 a 10240-face terrain
        emitted a 10241 px satellite, 25% past the limit (issue #197)."""
        for size in (129, 257, 513, 1025, 2049, 4097, 8193, 10241, 16385):
            x, z = compute_satellite_target_dims(size, size)
            assert x <= SATELLITE_MAX_DIM and z <= SATELLITE_MAX_DIM, (
                f"heightmap {size} produced satellite {x}x{z}, over the cap"
            )

    def test_satellite_at_least_heightmap_resolution_below_the_cap(self):
        """Below the cap the satellite must never lose detail against the
        heightmap — that is what the multiplier is for."""
        for size in (129, 257, 513, 1025, 2049):
            x, _ = compute_satellite_target_dims(size, size)
            if x < SATELLITE_MAX_DIM:
                assert x >= size, (
                    f"satellite dim {x} smaller than heightmap dim {size}"
                )
