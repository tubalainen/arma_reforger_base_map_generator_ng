"""
Terrain grid size derivation.

The Enfusion "New Terrain" dialog requires the terrain grid size (faces per
axis) to be a multiple of the 128-face tile size — NOT a power of two (Everon
is 6400). These tests pin snap_to_tile_multiple() and the size constants it
depends on.
"""

from config.enfusion import snap_to_tile_multiple
from config.terrain import (
    TERRAIN_TILE_FACES, MAX_TERRAIN_GRID_SIZE, MAX_MAP_EXTENT_M,
    DEFAULT_GRID_CELL_SIZE,
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


class TestTerrainConstants:
    def test_tile_is_128_faces(self):
        assert TERRAIN_TILE_FACES == 128

    def test_max_grid_size_is_a_tile_multiple(self):
        assert MAX_TERRAIN_GRID_SIZE % TERRAIN_TILE_FACES == 0

    def test_max_map_extent_matches_grid_and_cell(self):
        assert MAX_MAP_EXTENT_M == MAX_TERRAIN_GRID_SIZE * DEFAULT_GRID_CELL_SIZE
