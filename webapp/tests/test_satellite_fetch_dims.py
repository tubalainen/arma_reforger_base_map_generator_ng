"""
Satellite fetch sizing: the fetch must hold target density over the envelope.

For a terrain on a projected CRS the satellite is fetched in WGS84 over the
*envelope* of the projected rectangle, which covers more ground than the
terrain does. The fetch used to be clamped to ``SATELLITE_MAX_DIM`` — the
output cap — so the terrain occupied only ``1/ratio`` of the fetched pixels and
the reprojection upsampled the difference back out.

Measured envelope ratios are modest (1.006 at Froson, 1.026 in Skane, 1.139 in
the far north-east of Sweden), so this recovers ~0.5-3% of linear detail on a
typical map and ~14% at the extreme.

The fetch is now clamped to ``SATELLITE_MAX_FETCH_DIM`` instead. The output PNG
is unchanged: still capped at ``SATELLITE_MAX_DIM``.
"""

import math

import pytest

from services.satellite_service import (
    SATELLITE_MAX_DIM,
    SATELLITE_MAX_FETCH_DIM,
    SATELLITE_RESOLUTION_MULTIPLIER,
    compute_satellite_fetch_dims,
    compute_satellite_target_dims,
)


def _old_fetch_dims(target_x, target_z, ratio_x, ratio_y):
    """The pre-fix formula, clamped to the *output* cap."""
    return (
        min(SATELLITE_MAX_DIM, int(math.ceil(target_x * ratio_x))),
        min(SATELLITE_MAX_DIM, int(math.ceil(target_z * ratio_y))),
    )


# Terrain vertex counts for maps whose faces are multiples of 128.
VERTEX_COUNTS = [513, 1025, 1281, 2049, 2561, 3969, 5121, 7937]
# Envelope ratios: 1.0 is an unrotated grid, ~1.3 a strongly rotated high-latitude one.
RATIOS = [1.0, 1.02, 1.1, 1.25, 1.3]


class TestOutputCapUnchanged:
    """The deliverable must not shrink — or grow — as a result of this fix."""

    @pytest.mark.parametrize("verts", VERTEX_COUNTS)
    def test_target_dims_still_capped_at_max_dim(self, verts):
        x, z = compute_satellite_target_dims(verts, verts)
        assert x <= SATELLITE_MAX_DIM
        assert z <= SATELLITE_MAX_DIM

    @pytest.mark.parametrize("verts", VERTEX_COUNTS)
    def test_target_dims_are_the_documented_formula(self, verts):
        x, z = compute_satellite_target_dims(verts, verts)
        expected = max(
            min(SATELLITE_MAX_DIM, verts * SATELLITE_RESOLUTION_MULTIPLIER), verts
        )
        assert (x, z) == (expected, expected)

    @pytest.mark.parametrize("verts", VERTEX_COUNTS)
    def test_satellite_is_never_smaller_than_the_heightmap(self, verts):
        x, z = compute_satellite_target_dims(verts, verts)
        assert x >= verts and z >= verts


class TestFetchHoldsDensity:
    @pytest.mark.parametrize("verts", VERTEX_COUNTS)
    @pytest.mark.parametrize("ratio", RATIOS)
    def test_fetch_never_below_target(self, verts, ratio):
        """Fetching below target means upsampling — the defect being fixed."""
        tx, tz = compute_satellite_target_dims(verts, verts)
        fx, fz = compute_satellite_fetch_dims(tx, tz, ratio, ratio)
        assert fx >= tx and fz >= tz

    @pytest.mark.parametrize("verts", VERTEX_COUNTS)
    @pytest.mark.parametrize("ratio", RATIOS)
    def test_never_fetches_less_than_the_old_code(self, verts, ratio):
        """No map may lose resolution relative to the previous release."""
        tx, tz = compute_satellite_target_dims(verts, verts)
        new_x, new_z = compute_satellite_fetch_dims(tx, tz, ratio, ratio)
        old_x, old_z = _old_fetch_dims(tx, tz, ratio, ratio)
        assert new_x >= old_x and new_z >= old_z

    def test_the_double_clamp_case_actually_improves(self):
        """A 5 km Swedish map: the case the old formula degraded."""
        tx, tz = compute_satellite_target_dims(2561, 2561)
        assert (tx, tz) == (SATELLITE_MAX_DIM, SATELLITE_MAX_DIM)
        ratio = 1.2
        old_x, _ = _old_fetch_dims(tx, tz, ratio, ratio)
        new_x, _ = compute_satellite_fetch_dims(tx, tz, ratio, ratio)
        assert old_x == SATELLITE_MAX_DIM          # clamped -> under-density
        assert new_x == math.ceil(tx * ratio)      # full density over envelope
        assert new_x > old_x

    @pytest.mark.parametrize("ratio", RATIOS)
    def test_fetch_respects_its_own_cap(self, ratio):
        fx, fz = compute_satellite_fetch_dims(
            SATELLITE_MAX_DIM, SATELLITE_MAX_DIM, ratio, ratio,
        )
        assert fx <= SATELLITE_MAX_FETCH_DIM and fz <= SATELLITE_MAX_FETCH_DIM

    def test_absurd_ratio_is_bounded_not_unbounded(self):
        fx, fz = compute_satellite_fetch_dims(8192, 8192, 99.0, 99.0)
        assert (fx, fz) == (SATELLITE_MAX_FETCH_DIM, SATELLITE_MAX_FETCH_DIM)

    @pytest.mark.parametrize("ratio", [0.0, 0.5, 0.99])
    def test_ratio_below_one_never_shrinks_the_fetch(self, ratio):
        """A degenerate ratio must not be a back door to a smaller fetch."""
        fx, fz = compute_satellite_fetch_dims(4096, 4096, ratio, ratio)
        assert (fx, fz) == (4096, 4096)

    def test_rectangular_axes_are_independent(self):
        fx, fz = compute_satellite_fetch_dims(4000, 2000, 1.5, 1.1)
        assert fx == 6000
        assert fz == 2200


class TestFetchCapOrdering:
    def test_fetch_cap_exceeds_output_cap(self):
        """If these were equal the double-clamp defect would silently return."""
        assert SATELLITE_MAX_FETCH_DIM > SATELLITE_MAX_DIM
