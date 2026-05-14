"""
Tests for the Lantmäteriet STAC orthophoto helpers — focused on the two
seam/coverage bugs behind issue #65:

1. `_wgs84_bbox_to_epsg3006_envelope` must envelope all four WGS84 corners
   after projection, not just the SW+NE diagonal (the pre-v1.3.4 behaviour
   that left a strip of the user's selection outside the merge bbox and
   produced a vertical missing-data line).
2. `_cog_merge_rgb` must merge two adjacent COG tiles without a
   1-pixel-wide black seam at their shared boundary.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


# ---------------------------------------------------------------------------
# Envelope tests — pyproj only (no rasterio needed)
# ---------------------------------------------------------------------------

pyproj = pytest.importorskip("pyproj", reason="pyproj is required for projection tests")

from services.lantmateriet.stac_orthophoto_service import (  # noqa: E402
    _wgs84_bbox_to_epsg3006_envelope,
)


class TestWgs84BboxToEpsg3006Envelope:
    def test_envelope_contains_all_four_corner_projections(self):
        """The envelope must include every corner's projected coordinate —
        not only the SW/NE pair the old code used."""
        from pyproj import Transformer

        bbox = (15.0, 58.0, 16.0, 58.5)
        x_min, y_min, x_max, y_max = _wgs84_bbox_to_epsg3006_envelope(bbox)

        to_3006 = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True)
        w, s, e, n = bbox
        for lon, lat in [(w, s), (e, s), (e, n), (w, n)]:
            px, py = to_3006.transform(lon, lat)
            assert x_min - 1e-3 <= px <= x_max + 1e-3
            assert y_min - 1e-3 <= py <= y_max + 1e-3

    def test_envelope_strictly_larger_than_sw_ne_only_at_swedish_lat(self):
        """At Swedish latitudes, the trapezoid effect is non-trivial — the
        envelope must be measurably wider than the SW/NE-only bbox the old
        code produced."""
        from pyproj import Transformer

        bbox = (15.0, 65.0, 16.0, 65.5)
        x_min, y_min, x_max, y_max = _wgs84_bbox_to_epsg3006_envelope(bbox)

        to_3006 = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True)
        sw_x, sw_y = to_3006.transform(bbox[0], bbox[1])
        ne_x, ne_y = to_3006.transform(bbox[2], bbox[3])

        # Old envelope was (sw_x, sw_y, ne_x, ne_y). New envelope must extend
        # outward on at least one edge.
        wider = (x_min < sw_x - 1.0) or (x_max > ne_x + 1.0)
        taller = (y_min < sw_y - 1.0) or (y_max > ne_y + 1.0)
        assert wider or taller, (
            f"envelope {(x_min, y_min, x_max, y_max)} did not extend beyond "
            f"the SW/NE-only bbox {(sw_x, sw_y, ne_x, ne_y)} at lat ~65°"
        )


# ---------------------------------------------------------------------------
# Merge seam test — needs rasterio (Docker CI / sufficient dev env)
# ---------------------------------------------------------------------------

rasterio = pytest.importorskip("rasterio", reason="rasterio is required for merge tests")

from services.lantmateriet.stac_orthophoto_service import _cog_merge_rgb  # noqa: E402


def _write_tile(path: Path, x_min: float, y_max: float, size: int, res_m: float, fill: int) -> None:
    """Write a single uniform-colour 4-band GeoTIFF in EPSG:3006."""
    from rasterio.transform import from_origin

    transform = from_origin(x_min, y_max, res_m, res_m)
    data = np.full((4, size, size), fill, dtype=np.uint8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=4,
        dtype="uint8",
        crs="EPSG:3006",
        transform=transform,
    ) as ds:
        ds.write(data)


class TestCogMergeNoSeam:
    def test_two_adjacent_tiles_have_no_black_column(self, tmp_path: Path):
        """Two horizontally-adjacent uniform tiles (each a different colour)
        must merge without a black/zero column at their shared boundary."""
        tile_a = tmp_path / "tile_a.tif"
        tile_b = tmp_path / "tile_b.tif"
        res_m = 2.0
        size = 64

        # Tile A: x=[500000, 500128], filled with red value 200
        _write_tile(tile_a, x_min=500_000, y_max=6_400_128, size=size, res_m=res_m, fill=200)
        # Tile B: x=[500128, 500256], filled with red value 100
        _write_tile(tile_b, x_min=500_128, y_max=6_400_128, size=size, res_m=res_m, fill=100)

        # Merge over the full extent that spans both tiles.
        merged, _transform = _cog_merge_rgb(
            [str(tile_a), str(tile_b)],
            epsg3006_bounds=(500_000, 6_400_000, 500_256, 6_400_128),
            target_width=128,
            target_height=64,
        )

        assert merged is not None
        red_band = merged[0]
        # Every column must contain non-zero red samples — a black seam at
        # any x would show as an all-zero column.
        col_max = red_band.max(axis=0)
        zero_cols = int(np.sum(col_max == 0))
        assert zero_cols == 0, f"found {zero_cols} all-zero column(s) — seam present"
