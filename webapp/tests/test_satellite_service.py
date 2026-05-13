"""
Tests for services/satellite_service.py.

Focuses on reproject_satellite_to_terrain_crs and its handling of rectangular
(non-square) terrain outputs. Pre-v1.2.5 the function hardcoded a square output
grid, which corrupted rectangular maps (issue #58).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

# rasterio lives in the Docker image but not in dev environments without GDAL.
pytest.importorskip("rasterio", reason="rasterio is required for satellite reprojection tests")

from services.satellite_service import reproject_satellite_to_terrain_crs  # noqa: E402


# A small synthetic WGS84 bbox at mid-Sweden latitude (where EPSG:3006 is meaningful).
# Use a roughly 1° wide × 0.5° tall rectangle so the aspect is genuinely non-square.
SRC_BBOX = (15.0, 58.0, 16.0, 58.5)

# Projected destination bounds (SWEREF99 TM, EPSG:3006). The exact numbers don't
# matter for these tests — only that the aspect is the same as the requested
# pixel dimensions.
DST_CRS = "EPSG:3006"
DST_BOUNDS_2_TO_1 = (500_000.0, 6_400_000.0, 560_000.0, 6_430_000.0)  # 60 km × 30 km
DST_BOUNDS_SQUARE = (500_000.0, 6_400_000.0, 540_000.0, 6_440_000.0)  # 40 km × 40 km


def _write_synthetic_png(path: Path, width: int, height: int) -> None:
    """Write a colorful gradient PNG so reprojection produces non-zero output."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[..., 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    arr[..., 1] = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
    arr[..., 2] = 128
    Image.fromarray(arr).save(str(path), format="PNG")


class TestReprojectRectangular:
    def test_reproject_preserves_rectangular_dimensions(self, tmp_path: Path):
        """Rectangular target (800×400) must produce a rectangular PNG, not a square one."""
        png = tmp_path / "satellite_map.png"
        _write_synthetic_png(png, width=1000, height=500)

        ok = reproject_satellite_to_terrain_crs(
            satellite_path=png,
            src_bbox=SRC_BBOX,
            dst_crs=DST_CRS,
            dst_bounds=DST_BOUNDS_2_TO_1,
            target_size=(800, 400),
        )
        assert ok is True

        with Image.open(png) as out:
            assert out.size == (800, 400), (
                f"Reprojection must honour the requested (width, height) tuple; "
                f"pre-v1.2.5 it produced a square output, corrupting rectangular maps. "
                f"Got {out.size}."
            )

    def test_reproject_preserves_square_dimensions(self, tmp_path: Path):
        """Regression: square targets must still work (this is the v1.2.4 behaviour)."""
        png = tmp_path / "satellite_map.png"
        _write_synthetic_png(png, width=512, height=512)

        ok = reproject_satellite_to_terrain_crs(
            satellite_path=png,
            src_bbox=SRC_BBOX,
            dst_crs=DST_CRS,
            dst_bounds=DST_BOUNDS_SQUARE,
            target_size=(512, 512),
        )
        assert ok is True

        with Image.open(png) as out:
            assert out.size == (512, 512)

    def test_reproject_accepts_scalar_target_size(self, tmp_path: Path):
        """Backwards-compatibility: a scalar target_size should still produce a square output."""
        png = tmp_path / "satellite_map.png"
        _write_synthetic_png(png, width=512, height=512)

        ok = reproject_satellite_to_terrain_crs(
            satellite_path=png,
            src_bbox=SRC_BBOX,
            dst_crs=DST_CRS,
            dst_bounds=DST_BOUNDS_SQUARE,
            target_size=256,
        )
        assert ok is True

        with Image.open(png) as out:
            assert out.size == (256, 256)

    def test_reproject_tall_rectangle(self, tmp_path: Path):
        """Aspect must be respected when height > width too."""
        png = tmp_path / "satellite_map.png"
        _write_synthetic_png(png, width=400, height=800)

        # 30 km × 60 km — tall in the projected CRS
        dst_bounds_tall = (500_000.0, 6_400_000.0, 530_000.0, 6_460_000.0)
        ok = reproject_satellite_to_terrain_crs(
            satellite_path=png,
            src_bbox=(15.0, 58.0, 15.5, 59.0),
            dst_crs=DST_CRS,
            dst_bounds=dst_bounds_tall,
            target_size=(400, 800),
        )
        assert ok is True

        with Image.open(png) as out:
            assert out.size == (400, 800)


class TestReprojectFailure:
    def test_missing_file_returns_false(self, tmp_path: Path):
        """A missing source file must yield False so callers can warn instead of crashing."""
        ok = reproject_satellite_to_terrain_crs(
            satellite_path=tmp_path / "does_not_exist.png",
            src_bbox=SRC_BBOX,
            dst_crs=DST_CRS,
            dst_bounds=DST_BOUNDS_2_TO_1,
            target_size=(800, 400),
        )
        assert ok is False
