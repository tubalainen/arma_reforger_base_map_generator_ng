"""
Terrain grid size derivation.

The Enfusion "New Terrain" dialog requires the terrain grid size (faces per
axis) to be a multiple of the 128-face tile size — NOT a power of two (Everon
is 6400). These tests pin snap_to_tile_multiple() and the size constants it
depends on.
"""

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
