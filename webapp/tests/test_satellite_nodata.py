"""
Satellite imagery holes — a lost tile composites as solid black, not an error.

Found on a real Swedish map (SE_59N_14E): 7% of satellite_map.png was a single
pure-black 2154x1753 px block in the SE corner, and every check we had passed
it. The raster contract only looked at dimensions and mode, and STAC Bild's own
guard counted *failed tiles* rather than the coverage of the result.

Counting attempts is not a proxy for coverage. A few failed tiles are usually
harmless because an overlapping older tile fills the gap, but an edge tile has
nothing behind it — so two failures out of many, comfortably inside the
one-third tile budget, still left a hole a third of the image wide.

Pure black is an unambiguous nodata signal: measured on that same orthophoto,
the natural rate of all-three-bands-zero pixels outside the hole is *exactly
zero*. Even deep lake water bottoms out at (0, 16, 12).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


def _rasters(tmp: Path, faces_x: int, faces_z: int, sat: np.ndarray) -> None:
    (tmp / "heightmap.asc").write_text(
        f"ncols {faces_x + 1}\nnrows {faces_z + 1}\n"
        "xllcorner 0\nyllcorner 0\ncellsize 2\nNODATA_value -9999\n"
    )
    Image.new("L", (faces_x, faces_z)).save(tmp / "surface_grass.png")
    Image.fromarray(sat).save(tmp / "satellite_map.png")


def _filled(h: int, w: int, value: int = 120) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


class TestNodataFractionHelper:
    def test_a_clean_mosaic_is_zero(self):
        from services.lantmateriet.stac_orthophoto_service import nodata_fraction

        assert nodata_fraction(np.full((3, 50, 50), 200, dtype=np.uint8)) == 0.0

    def test_a_corner_hole_is_measured(self):
        from services.lantmateriet.stac_orthophoto_service import nodata_fraction

        a = np.full((3, 100, 100), 200, dtype=np.uint8)
        a[:, 90:, 90:] = 0
        assert nodata_fraction(a) == pytest.approx(0.01)

    def test_zero_in_one_band_only_is_not_nodata(self):
        """Dark water is (0, 16, 12) in the real orthophoto — a zero red
        channel must not be mistaken for a missing tile."""
        from services.lantmateriet.stac_orthophoto_service import nodata_fraction

        a = np.full((3, 20, 20), 50, dtype=np.uint8)
        a[0] = 0
        assert nodata_fraction(a) == 0.0

    def test_missing_mosaic_counts_as_fully_empty(self):
        from services.lantmateriet.stac_orthophoto_service import nodata_fraction

        assert nodata_fraction(None) == 1.0
        assert nodata_fraction(np.zeros((3, 0, 0), dtype=np.uint8)) == 1.0


class TestRasterContractCatchesHoles:
    def test_the_real_seN59E14_void_is_caught(self):
        """7% black in one corner — the exact case that shipped."""
        from services.raster_contract import validate_and_harden_rasters

        tmp = Path(tempfile.mkdtemp())
        sat = _filled(2564, 5124)
        # Same proportions as the real void: 32.6% of the width by 21.4% of
        # the height, in the corner = 6.96% of the image.
        sat[2015:, 3454:] = 0
        _rasters(tmp, 1280, 640, sat)

        report = validate_and_harden_rasters(tmp, 1280, 640)
        assert not report["ok"], "a 7% black corner must not pass the contract"
        assert any("pure black" in str(i) for i in report["issues"])
        assert report["satellite"]["nodata_fraction"] > 0.05

    def test_a_clean_satellite_passes_and_records_zero(self):
        from services.raster_contract import validate_and_harden_rasters

        tmp = Path(tempfile.mkdtemp())
        _rasters(tmp, 1280, 640, _filled(2564, 5124))

        report = validate_and_harden_rasters(tmp, 1280, 640)
        assert report["ok"], report["issues"]
        assert report["satellite"]["nodata_fraction"] == 0.0

    def test_a_few_stray_black_pixels_do_not_trip_it(self):
        """Compression artifacts must not be reported as a missing tile."""
        from services.raster_contract import validate_and_harden_rasters

        tmp = Path(tempfile.mkdtemp())
        sat = _filled(2564, 5124)
        sat[:10, :10] = 0  # 100 px out of 13.1M
        _rasters(tmp, 1280, 640, sat)

        report = validate_and_harden_rasters(tmp, 1280, 640)
        assert report["ok"], report["issues"]

    def test_the_threshold_is_where_we_think_it_is(self):
        """Pin the boundary from both sides so a future edit can't quietly
        widen it into uselessness."""
        from services.raster_contract import (
            SATELLITE_MAX_NODATA_FRACTION,
            validate_and_harden_rasters,
        )

        h, w = 1000, 1000
        for fraction, should_pass in ((0.002, True), (0.02, False)):
            tmp = Path(tempfile.mkdtemp())
            sat = _filled(h, w)
            rows = int(h * fraction)
            sat[:rows, :] = 0
            _rasters(tmp, w, h, sat)
            report = validate_and_harden_rasters(tmp, w, h)
            assert report["ok"] is should_pass, (
                f"{fraction:.1%} black vs limit "
                f"{SATELLITE_MAX_NODATA_FRACTION:.1%}: ok={report['ok']}"
            )


class TestSetupGuideSurfacesTheHole:
    def _guide(self, void: float) -> str:
        from services.setup_guide_generator import SetupGuideGenerator

        meta = {
            "heightmap": {
                "dimensions": "1281x641",
                "grid_cell_size_m": 2.0,
                "terrain_size_m": "2560x1280",
            },
            "elevation": {
                "min_elevation_m": 0.0,
                "max_elevation_m": 100.0,
                "dialog_height_scale": 0.03125,
            },
            "satellite": {"file": "satellite_map.png", "source": "STAC Bild"},
            "raster_validation": {"satellite": {"nodata_fraction": void}},
            "input": {
                "bbox": {"south": 59.0, "north": 59.3, "west": 13.4, "east": 13.6},
                "countries": ["SE"],
            },
        }
        out = Path(tempfile.mkdtemp())
        SetupGuideGenerator(map_name="M", metadata=meta).generate(out)
        return (out / "SETUP_GUIDE.md").read_text(encoding="utf-8")

    def test_a_hole_is_called_out_before_the_import_steps(self):
        guide = self._guide(0.069606)
        assert "satellite image has a gap" in guide
        assert "7%" in guide
        # Must appear in Phase 5, above the import instructions.
        phase = guide.index("## Phase 5")
        step = guide.index("Step 4.1", phase)
        assert phase < guide.index("satellite image has a gap") < step

    def test_a_clean_map_says_nothing(self):
        assert "satellite image has a gap" not in self._guide(0.0)

    def test_a_negligible_amount_says_nothing(self):
        assert "satellite image has a gap" not in self._guide(0.001)
