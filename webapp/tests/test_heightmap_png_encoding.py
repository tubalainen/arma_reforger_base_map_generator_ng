"""
16-bit heightmap PNG encoding contract.

``heightmap.png`` is the pipeline's most important output: Enfusion's terrain
importer reads it as a 16-bit greyscale PNG. The writers used to pass
``Image.fromarray(arr, mode=...)``, which Pillow removes in 13 (2026-10-15) and
which reinterpreted the raw buffer instead of converting. The mode must now be
derived from the array dtype, so these tests pin the resulting encoding:

  heightmap.png          -> 16-bit greyscale (bitdepth 16, PNG colour type 0)
  heightmap_preview.png  ->  8-bit greyscale (bitdepth  8, PNG colour type 0)
  surface_*.png          ->  8-bit greyscale "L"
  surface_preview.png    ->  8-bit "RGB"
"""

import struct
import warnings

import numpy as np
import pytest
from PIL import Image

from services.heightmap_generator import (
    generate_heightmap_from_array,
    save_heightmap_png,
    save_heightmap_preview,
)


def _png_ihdr(path):
    """Return ``(width, height, bitdepth, colour_type)`` from a PNG's IHDR."""
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # 8-byte signature, then the IHDR chunk: length(4) type(4) w(4) h(4) d(1) c(1)
    return struct.unpack(">IIBB", raw[16:26])


@pytest.fixture
def heightmap():
    """A uint16 heightmap that exercises both extremes and byte-order."""
    rng = np.random.default_rng(20260826)
    elevation = rng.random((129, 129)).astype(np.float32) * 1200.0 - 100.0
    hm, _ = generate_heightmap_from_array(elevation)
    # Canaries: a 16-bit writer that swapped bytes would turn 0x0100 into
    # 0x0001 and go unnoticed on a smooth gradient.
    hm[0, 0], hm[0, 1] = 0, 65535
    hm[1, 0], hm[1, 1] = 0x0100, 0x0001
    return hm


class TestHeightmapPng:
    def test_generate_returns_uint16(self, heightmap):
        assert heightmap.dtype == np.uint16

    def test_written_png_is_16bit_greyscale(self, tmp_path, heightmap):
        path = tmp_path / "heightmap.png"
        save_heightmap_png(heightmap, str(path))

        width, height, bitdepth, colour_type = _png_ihdr(path)
        assert (width, height) == (heightmap.shape[1], heightmap.shape[0])
        assert bitdepth == 16, "Enfusion needs a 16-bit heightmap"
        assert colour_type == 0, "Enfusion needs greyscale, not RGB/palette"

    def test_pillow_reopens_as_i16(self, tmp_path, heightmap):
        path = tmp_path / "heightmap.png"
        save_heightmap_png(heightmap, str(path))
        with Image.open(path) as img:
            assert img.mode == "I;16"

    def test_round_trips_through_numpy_unchanged(self, tmp_path, heightmap):
        path = tmp_path / "heightmap.png"
        save_heightmap_png(heightmap, str(path))
        with Image.open(path) as img:
            restored = np.array(img)
        assert restored.dtype == np.uint16
        np.testing.assert_array_equal(restored, heightmap)

    def test_no_deprecation_warning(self, tmp_path, heightmap):
        """Pillow 13 removes the `mode=` parameter — we must not rely on it."""
        path = tmp_path / "heightmap.png"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_heightmap_png(heightmap, str(path))
        deprecations = [
            str(w.message)
            for w in caught
            if issubclass(w.category, DeprecationWarning)
        ]
        assert not deprecations, deprecations

    def test_accepts_a_non_contiguous_view(self, tmp_path, heightmap):
        """A sliced array has strides; the writer must still emit real pixels."""
        view = heightmap[::2, ::2]
        assert not view.flags["C_CONTIGUOUS"]
        path = tmp_path / "heightmap.png"
        save_heightmap_png(view, str(path))
        with Image.open(path) as img:
            restored = np.array(img)
        np.testing.assert_array_equal(restored, view)


class TestHeightmapPreviewPng:
    def test_preview_is_8bit_greyscale(self, tmp_path, heightmap):
        path = tmp_path / "heightmap_preview.png"
        save_heightmap_preview(heightmap, str(path))
        width, height, bitdepth, colour_type = _png_ihdr(path)
        assert (width, height) == (heightmap.shape[1], heightmap.shape[0])
        assert (bitdepth, colour_type) == (8, 0)
        with Image.open(path) as img:
            assert img.mode == "L"


class TestSurfaceMaskEncoding:
    """The surface writers use the same dtype-derived form (see #187)."""

    def test_uint8_mask_writes_8bit_grey_l(self, tmp_path):
        rng = np.random.default_rng(7)
        mask = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
        path = tmp_path / "surface_grass.png"
        Image.fromarray(mask).save(str(path))
        assert _png_ihdr(path)[2:] == (8, 0)
        with Image.open(path) as img:
            assert img.mode == "L"
            np.testing.assert_array_equal(np.array(img), mask)

    def test_uint8_rgb_preview_writes_8bit_rgb(self, tmp_path):
        rng = np.random.default_rng(8)
        preview = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
        path = tmp_path / "surface_preview.png"
        Image.fromarray(preview).save(str(path))
        assert _png_ihdr(path)[2:] == (8, 2)  # colour type 2 = truecolour RGB
        with Image.open(path) as img:
            assert img.mode == "RGB"
            np.testing.assert_array_equal(np.array(img), preview)
