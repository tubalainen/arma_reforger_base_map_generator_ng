"""
Raster dimension/encoding contract (issues #100/#111/#115/#138).

For a terrain of N faces per axis:
  heightmap.asc  -> (N+1) x (N+1)
  surface_*.png  -> N x N, 8-bit grayscale "L"
  satellite      -> square, plain RGB (no alpha/ICC)
"""

import numpy as np
from PIL import Image

from services.raster_contract import (
    parse_asc_header_dims,
    validate_and_harden_rasters,
)

FACES = 256  # one example terrain grid size (a tile multiple)


def _write_asc(path, ncols, nrows):
    with open(path, "w") as f:
        f.write(f"ncols         {ncols}\n")
        f.write(f"nrows         {nrows}\n")
        f.write("xllcorner     0.0\n")
        f.write("yllcorner     0.0\n")
        f.write("cellsize      2.0\n")
        f.write("NODATA_value  -9999\n")
        f.write("0.0 0.0\n")


def _write_mask(path, w, h, mode="L"):
    arr = np.zeros((h, w), dtype=np.uint8)
    Image.fromarray(arr, mode="L").convert(mode).save(str(path))


def _good_project(tmp_path):
    _write_asc(tmp_path / "heightmap.asc", FACES + 1, FACES + 1)
    _write_mask(tmp_path / "surface_grass.png", FACES, FACES)
    _write_mask(tmp_path / "surface_asphalt.png", FACES, FACES)
    Image.new("RGB", (1024, 1024)).save(str(tmp_path / "satellite_map.png"))


class TestParseAscHeader:
    def test_reads_ncols_nrows(self, tmp_path):
        _write_asc(tmp_path / "h.asc", 513, 513)
        assert parse_asc_header_dims(tmp_path / "h.asc") == (513, 513)


class TestContractHappyPath:
    def test_well_formed_project_is_ok(self, tmp_path):
        _good_project(tmp_path)
        report = validate_and_harden_rasters(tmp_path, FACES, FACES)
        assert report["ok"] is True
        assert report["issues"] == []
        assert report["heightmap"] == {"ncols": FACES + 1, "nrows": FACES + 1}

    def test_preview_png_is_ignored(self, tmp_path):
        _good_project(tmp_path)
        # An RGB preview at a different size must not trip the mask check.
        Image.new("RGB", (512, 512)).save(str(tmp_path / "surface_preview.png"))
        report = validate_and_harden_rasters(tmp_path, FACES, FACES)
        assert report["ok"] is True


class TestContractDetectsDefects:
    def test_vertex_resolution_mask_is_flagged(self, tmp_path):
        # The #100 regression: masks written at N+1 instead of N.
        _good_project(tmp_path)
        _write_mask(tmp_path / "surface_grass.png", FACES + 1, FACES + 1)
        report = validate_and_harden_rasters(tmp_path, FACES, FACES)
        assert report["ok"] is False
        assert any("surface_grass.png" in i for i in report["issues"])

    def test_wrong_heightmap_size_is_flagged(self, tmp_path):
        _good_project(tmp_path)
        _write_asc(tmp_path / "heightmap.asc", FACES, FACES)  # forgot the +1
        report = validate_and_harden_rasters(tmp_path, FACES, FACES)
        assert report["ok"] is False
        assert any("heightmap.asc" in i for i in report["issues"])


class TestEncodingHardening:
    def test_rgb_mask_is_converted_to_grayscale(self, tmp_path):
        _good_project(tmp_path)
        Image.new("RGB", (FACES, FACES)).save(str(tmp_path / "surface_dirt.png"))
        report = validate_and_harden_rasters(tmp_path, FACES, FACES)
        with Image.open(tmp_path / "surface_dirt.png") as img:
            assert img.mode == "L"
        assert any("surface_dirt.png" in fx for fx in report["fixes"])
        # Converting encoding must not, by itself, make the project not-ok.
        assert report["ok"] is True

    def test_rgba_satellite_is_flattened_to_rgb(self, tmp_path):
        _good_project(tmp_path)
        Image.new("RGBA", (1024, 1024)).save(str(tmp_path / "satellite_map.png"))
        report = validate_and_harden_rasters(tmp_path, FACES, FACES)
        with Image.open(tmp_path / "satellite_map.png") as img:
            assert img.mode == "RGB"
        assert report["ok"] is True
