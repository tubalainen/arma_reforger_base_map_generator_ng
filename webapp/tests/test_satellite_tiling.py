"""
Tiled Sentinel-2 fetching (issue #97).

EOX's WMS rejects any GetMap above 4096 px per axis with HTTP 400. Since v1.3.6
the satellite target has been 4x the heightmap capped at 8192, so every
non-Swedish map above ~2 km asked for more than 4096 and got no imagery at all.

The fix tiles the request and stitches the result. The output is the full
requested size — tiling must never become a downgrade, so these tests check the
mosaic is pixel-exact, not merely the right shape.
"""

import io
import math

import numpy as np
import pytest
from PIL import Image

from services.satellite_service import (
    EOX_WMS_MAX_DIM,
    _stitch_tiles,
    _sub_bbox,
    fetch_sentinel2_cloudless,
    split_pixel_axis,
)

BBOX = (14.0, 58.0, 14.5, 58.4)  # west, south, east, north


class TestSplitPixelAxis:
    @pytest.mark.parametrize("total", [1, 100, 4095, 4096])
    def test_small_axis_is_a_single_range(self, total):
        assert split_pixel_axis(total, EOX_WMS_MAX_DIM) == [(0, total)]

    def test_just_over_the_limit_splits_in_two(self):
        assert split_pixel_axis(4097, 4096) == [(0, 2048), (2048, 4097)]

    @pytest.mark.parametrize("total", [4097, 5124, 8192, 8437, 10244, 16384])
    def test_ranges_tile_the_axis_exactly(self, total):
        ranges = split_pixel_axis(total, EOX_WMS_MAX_DIM)
        assert ranges[0][0] == 0
        assert ranges[-1][1] == total
        for (_, prev_end), (next_start, _) in zip(ranges, ranges[1:]):
            assert prev_end == next_start, "gap or overlap between tiles"
        assert sum(b - a for a, b in ranges) == total

    @pytest.mark.parametrize("total", [4097, 5124, 8192, 8437, 10244, 16384])
    def test_no_range_exceeds_the_server_limit(self, total):
        for a, b in split_pixel_axis(total, EOX_WMS_MAX_DIM):
            assert 0 < b - a <= EOX_WMS_MAX_DIM

    @pytest.mark.parametrize("total", [4097, 8192, 10244])
    def test_uses_the_fewest_tiles_possible(self, total):
        assert len(split_pixel_axis(total, EOX_WMS_MAX_DIM)) == math.ceil(
            total / EOX_WMS_MAX_DIM
        )


class TestSubBbox:
    def test_single_full_range_is_the_original_bbox(self):
        got = _sub_bbox(BBOX, 100, 80, (0, 100), (0, 80))
        assert got == pytest.approx(BBOX)

    def test_row_zero_is_the_north_edge(self):
        """Pixel row 0 is north; getting this backwards flips the imagery."""
        west, south, east, north = BBOX
        top = _sub_bbox(BBOX, 100, 80, (0, 100), (0, 40))
        bottom = _sub_bbox(BBOX, 100, 80, (0, 100), (40, 80))
        assert top[3] == pytest.approx(north)
        assert bottom[1] == pytest.approx(south)
        assert top[1] == pytest.approx(bottom[3])  # they meet in the middle

    def test_tiles_cover_the_bbox_without_gaps(self):
        w, h = 8192, 8192
        xs = split_pixel_axis(w, EOX_WMS_MAX_DIM)
        ys = split_pixel_axis(h, EOX_WMS_MAX_DIM)
        boxes = [_sub_bbox(BBOX, w, h, xr, yr) for yr in ys for xr in xs]
        assert min(b[0] for b in boxes) == pytest.approx(BBOX[0])
        assert min(b[1] for b in boxes) == pytest.approx(BBOX[1])
        assert max(b[2] for b in boxes) == pytest.approx(BBOX[2])
        assert max(b[3] for b in boxes) == pytest.approx(BBOX[3])

    @pytest.mark.parametrize("w,h", [(8192, 8192), (8437, 8437), (10244, 5124)])
    def test_tile_bbox_aspect_matches_pixel_aspect(self, w, h):
        """EOX returns HTTP 400 when bbox aspect and WIDTH:HEIGHT disagree."""
        lon_per_px = (BBOX[2] - BBOX[0]) / w
        lat_per_px = (BBOX[3] - BBOX[1]) / h
        for yr in split_pixel_axis(h, EOX_WMS_MAX_DIM):
            for xr in split_pixel_axis(w, EOX_WMS_MAX_DIM):
                west, south, east, north = _sub_bbox(BBOX, w, h, xr, yr)
                assert (east - west) == pytest.approx(lon_per_px * (xr[1] - xr[0]))
                assert (north - south) == pytest.approx(lat_per_px * (yr[1] - yr[0]))


def _source_image(w, h):
    """A gradient with unique per-pixel values, so misplacement is detectable."""
    xs = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    ys = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[..., 0] = xs
    arr[..., 1] = ys
    arr[..., 2] = ((xs.astype(np.int16) + ys.astype(np.int16)) // 2).astype(np.uint8)
    return Image.fromarray(arr)


class TestStitching:
    @pytest.mark.parametrize("w,h", [(8192, 8192), (8437, 6000), (5000, 4097)])
    def test_stitched_mosaic_is_pixel_identical_to_the_source(self, w, h):
        src = _source_image(w, h)
        xs = split_pixel_axis(w, EOX_WMS_MAX_DIM)
        ys = split_pixel_axis(h, EOX_WMS_MAX_DIM)

        tiles = {}
        for col, (x0, x1) in enumerate(xs):
            for row, (y0, y1) in enumerate(ys):
                buf = io.BytesIO()
                src.crop((x0, y0, x1, y1)).save(buf, format="PNG")
                tiles[(col, row)] = buf.getvalue()

        out = _stitch_tiles(tiles, xs, ys, w, h)
        with Image.open(io.BytesIO(out)) as got:
            assert got.size == (w, h)
            np.testing.assert_array_equal(np.array(got.convert("RGB")), np.array(src))

    def test_jpeg_tiles_are_accepted(self):
        """EOX answers image/jpeg even when FORMAT=image/png is requested."""
        w = h = 5000
        src = _source_image(w, h)
        xs, ys = split_pixel_axis(w, EOX_WMS_MAX_DIM), split_pixel_axis(h, EOX_WMS_MAX_DIM)
        tiles = {}
        for col, (x0, x1) in enumerate(xs):
            for row, (y0, y1) in enumerate(ys):
                buf = io.BytesIO()
                src.crop((x0, y0, x1, y1)).save(buf, format="JPEG", quality=95)
                tiles[(col, row)] = buf.getvalue()
        out = _stitch_tiles(tiles, xs, ys, w, h)
        with Image.open(io.BytesIO(out)) as got:
            assert got.size == (w, h) and got.mode == "RGB"


class TestFetchIntegration:
    """Drive fetch_sentinel2_cloudless with the network stubbed out."""

    @pytest.fixture
    def stub(self, monkeypatch):
        calls = []

        async def fake_window(client, bbox, width, height):
            calls.append({"bbox": bbox, "width": width, "height": height})
            buf = io.BytesIO()
            Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
            return buf.getvalue()

        monkeypatch.setattr(
            "services.satellite_service._fetch_sentinel2_window", fake_window
        )
        return calls

    @pytest.mark.asyncio
    async def test_small_request_uses_a_single_call(self, stub):
        data = await fetch_sentinel2_cloudless(BBOX, 2048, 2048)
        assert data is not None
        assert len(stub) == 1
        assert (stub[0]["width"], stub[0]["height"]) == (2048, 2048)

    @pytest.mark.asyncio
    async def test_the_issue_97_case_now_succeeds(self, stub):
        """8192x8192 — the exact request in the #97 log — must return imagery."""
        data = await fetch_sentinel2_cloudless(BBOX, 8192, 8192)
        assert data is not None
        with Image.open(io.BytesIO(data)) as img:
            assert img.size == (8192, 8192)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dim", [4097, 5124, 8192, 10244])
    async def test_no_request_ever_exceeds_the_server_limit(self, stub, dim):
        await fetch_sentinel2_cloudless(BBOX, dim, dim)
        assert stub, "no requests made"
        for call in stub:
            assert call["width"] <= EOX_WMS_MAX_DIM
            assert call["height"] <= EOX_WMS_MAX_DIM

    @pytest.mark.asyncio
    async def test_output_is_the_full_requested_size_not_clamped(self, stub):
        """The wrong fix would clamp to 4096; the output must stay 8192."""
        data = await fetch_sentinel2_cloudless(BBOX, 8192, 8192)
        with Image.open(io.BytesIO(data)) as img:
            assert img.size != (EOX_WMS_MAX_DIM, EOX_WMS_MAX_DIM)
            assert img.size == (8192, 8192)

    @pytest.mark.asyncio
    async def test_rectangular_request_is_preserved(self, stub):
        data = await fetch_sentinel2_cloudless(BBOX, 8192, 5124)
        with Image.open(io.BytesIO(data)) as img:
            assert img.size == (8192, 5124)

    @pytest.mark.asyncio
    async def test_a_permanently_failing_tile_yields_no_mosaic(self, monkeypatch):
        """A hole would import as a black rectangle — better to return nothing."""
        async def flaky(client, bbox, width, height):
            if bbox[0] > BBOX[0]:      # every tile except the westernmost column
                raise RuntimeError("boom")
            buf = io.BytesIO()
            Image.new("RGB", (width, height)).save(buf, format="PNG")
            return buf.getvalue()

        monkeypatch.setattr(
            "services.satellite_service._fetch_sentinel2_window", flaky
        )
        assert await fetch_sentinel2_cloudless(BBOX, 8192, 8192) is None

    @pytest.mark.asyncio
    async def test_a_tile_that_recovers_on_the_serial_sweep_still_succeeds(
        self, monkeypatch
    ):
        state = {"failed_once": False}

        async def flaky(client, bbox, width, height):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("transient")
            buf = io.BytesIO()
            Image.new("RGB", (width, height)).save(buf, format="PNG")
            return buf.getvalue()

        monkeypatch.setattr(
            "services.satellite_service._fetch_sentinel2_window", flaky
        )
        data = await fetch_sentinel2_cloudless(BBOX, 8192, 8192)
        assert data is not None, "the serial retry sweep should have recovered it"
