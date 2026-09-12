"""
Terrain grid size derivation.

The Enfusion "New Terrain" dialog requires the terrain grid size (faces per
axis) to be a multiple of the 128-face tile size — NOT a power of two (Everon
is 6400). These tests pin snap_to_tile_multiple() and the size constants it
depends on.
"""

import math

from config.enfusion import snap_to_tile_multiple, pick_clean_height_scale
from config.terrain import (
    TERRAIN_TILE_FACES, MAX_TERRAIN_GRID_SIZE, MAX_MAP_EXTENT_M,
    DEFAULT_GRID_CELL_SIZE, DEFAULT_HEIGHT_SCALE,
)


class TestSnapToTileMultiple:
    def test_exact_multiples_unchanged(self):
        for n in (128, 256, 2048, 4096, 6400, 8192, 16384):
            assert snap_to_tile_multiple(n) == n

    def test_non_power_of_two_is_valid(self):
        # 6400 (Everon's grid size) was impossible under the old power-of-2
        # restriction; it must now round-trip unchanged.
        assert snap_to_tile_multiple(6400) == 6400

    def test_rounds_to_nearest_tile(self):
        assert snap_to_tile_multiple(4000) == 3968   # 31.25 tiles -> 31
        assert snap_to_tile_multiple(4050) == 4096   # 31.64 tiles -> 32
        assert snap_to_tile_multiple(200) == 256     # 1.56 tiles -> 2

    def test_result_always_multiple_of_128(self):
        for raw in (1, 100, 333, 5000, 12801, 99999):
            assert snap_to_tile_multiple(raw) % TERRAIN_TILE_FACES == 0

    def test_clamped_to_minimum(self):
        assert snap_to_tile_multiple(0) == TERRAIN_TILE_FACES
        assert snap_to_tile_multiple(1) == TERRAIN_TILE_FACES
        assert snap_to_tile_multiple(-500) == TERRAIN_TILE_FACES

    def test_clamped_to_maximum(self):
        assert snap_to_tile_multiple(20_000) == MAX_TERRAIN_GRID_SIZE
        assert snap_to_tile_multiple(10**9) == MAX_TERRAIN_GRID_SIZE


class TestPickCleanHeightScale:
    """Issue #142 — the New Terrain "Height scale" must be a clean, typeable
    value (default 0.03125), not the old un-typeable ``range / 65535`` fraction.
    Heightmaps import as absolute metres (sea = 0) with Resample off, so the
    scale only has to *represent* the span, not rescale it."""

    def test_142_example_returns_engine_default(self):
        # The exact range from issue #142 (22.4 m – 34.5 m).
        assert pick_clean_height_scale(22.4, 34.5) == DEFAULT_HEIGHT_SCALE

    def test_typical_maps_use_default(self):
        for mn, mx in [(0.0, 1.0), (0.0, 1000.0), (-30.0, 1200.0), (200.0, 800.0)]:
            assert pick_clean_height_scale(mn, mx) == DEFAULT_HEIGHT_SCALE

    def test_climbs_ladder_when_span_exceeds_default_band(self):
        # max above the default +1843 m ceiling -> next clean value.
        assert pick_clean_height_scale(400.0, 1900.0) == 0.0625
        # seabed below the default -205 m floor -> climb as well.
        assert pick_clean_height_scale(-300.0, 1500.0) == 0.0625

    def test_result_always_represents_the_span(self):
        for mn, mx in [(22.4, 34.5), (400.0, 1900.0), (-300.0, 1500.0),
                       (0.0, 6000.0), (-1000.0, 5000.0)]:
            hs = pick_clean_height_scale(mn, mx)
            upper = hs * 65535.0 * 0.9
            lower = -hs * 65535.0 * 0.1
            assert mx <= upper + 1e-6
            assert mn >= lower - 1e-6

    def test_never_below_engine_default(self):
        # A flat map must not produce a tiny (un-typeable) scale.
        assert pick_clean_height_scale(50.0, 50.01) >= DEFAULT_HEIGHT_SCALE


class TestTerrainConstants:
    def test_tile_is_128_faces(self):
        assert TERRAIN_TILE_FACES == 128

    def test_max_grid_size_is_a_tile_multiple(self):
        assert MAX_TERRAIN_GRID_SIZE % TERRAIN_TILE_FACES == 0

    def test_max_map_extent_matches_grid_and_cell(self):
        assert MAX_MAP_EXTENT_M == MAX_TERRAIN_GRID_SIZE * DEFAULT_GRID_CELL_SIZE


class TestSnapRoundingMatchesTheBrowser:
    """The browser snaps the drawn box to what it computes, so if the two
    disagree the user sees one terrain on the map and gets another in the ZIP,
    silently. Reported by OrcVole on issue #197.

    Two separate causes, both fixed in v1.17.1:
      * Python's round() is banker's (2.5 -> 2) while Math.round() is half-up
        (2.5 -> 3), so every ODD half-tile diverged.
      * map_generator rounded metres -> faces before snapping faces -> tiles,
        a double rounding the browser's single step does not do.
    """

    def test_exact_half_tiles_round_up(self):
        from config.enfusion import snap_to_tile_multiple

        # 2.5 tiles must go to 3, not down to 2 (banker's rounding).
        assert snap_to_tile_multiple(128 * 2.5) == 128 * 3
        assert snap_to_tile_multiple(128 * 4.5) == 128 * 5
        # 3.5 -> 4 agreed even before the fix; pin it so a "fix" can't flip it.
        assert snap_to_tile_multiple(128 * 3.5) == 128 * 4

    def test_a_640m_axis_does_not_shrink_to_512m(self):
        """The exact case from the issue: 640 m is 2.5 tiles at 2 m cells."""
        faces = snap_to_tile_multiple(640 / DEFAULT_GRID_CELL_SIZE)
        assert faces * DEFAULT_GRID_CELL_SIZE == 768

    def test_fractional_face_counts_are_not_pre_rounded(self):
        """383 m is 191.5 faces. Pre-rounding gives 192 = exactly 1.5 tiles,
        which then rounds up to 2; the single step gives 1.496 -> 1."""
        from config.enfusion import snap_to_tile_multiple

        assert snap_to_tile_multiple(383 / 2) == 128

    def test_agrees_with_the_browser_across_the_whole_range(self):
        """Sweep every metre from 100 m to 40 km against a direct port of
        deriveAxis(). Any divergence is a terrain the user did not draw."""
        from config.enfusion import (
            snap_to_tile_multiple,
            TERRAIN_TILE_FACES,
            MAX_TERRAIN_GRID_SIZE,
        )
        from config.terrain import DEFAULT_GRID_CELL_SIZE

        def derive_axis_js(metres: float) -> int:
            # Math.round() semantics: halves away from zero, positive here.
            raw = metres / DEFAULT_GRID_CELL_SIZE / TERRAIN_TILE_FACES
            tiles = min(
                MAX_TERRAIN_GRID_SIZE // TERRAIN_TILE_FACES,
                max(1, math.floor(raw + 0.5)),
            )
            return tiles * TERRAIN_TILE_FACES

        bad = [
            m
            for m in range(100, 40_001)
            if snap_to_tile_multiple(m / DEFAULT_GRID_CELL_SIZE)
            != derive_axis_js(m)
        ]
        assert not bad, f"{len(bad)} sizes disagree with the browser, e.g. {bad[:5]}"
