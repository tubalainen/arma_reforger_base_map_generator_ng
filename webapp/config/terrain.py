"""Arma Reforger terrain defaults."""

# Grid cell size is locked to the Bohemia Interactive 2 m standard. Structures
# are designed for a 2 m terrain grid; coarser cells make asset placement
# unreliable (the BI wiki calls 4 m "impossible without holes"). This is no
# longer a user-facing choice — the generator always produces 2 m terrain.
DEFAULT_GRID_CELL_SIZE = 2         # metres per face

# Enfusion builds terrain from tiles; one tile is 128 faces
# (BLOCKS_PER_TILE 4 × BLOCK_FACE_SIZE 32, per the "New Terrain" dialog).
# Terrain grid size must be an integer multiple of this.
TERRAIN_TILE_FACES = 128

# Largest terrain grid size we allow (faces per axis). 16384 faces at 2 m is a
# 32.768 km map; the heightmap PNG is ~0.5 GB at that size.
MAX_TERRAIN_GRID_SIZE = 16384

# Max map extent per axis (metres) = largest terrain grid size × cell size.
MAX_MAP_EXTENT_M = MAX_TERRAIN_GRID_SIZE * DEFAULT_GRID_CELL_SIZE  # 32768

DEFAULT_HEIGHT_SCALE = 0.03125     # maps 0-65535 to ~0-2048 m
ENFUSION_HEIGHT_SCALE_DEFAULT = DEFAULT_HEIGHT_SCALE
ENFUSION_MAX_SURFACES_PER_BLOCK = 5
DEFAULT_TARGET_CRS = "EPSG:4326"
