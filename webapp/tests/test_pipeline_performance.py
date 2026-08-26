"""
Guards for the hot paths rewritten in v1.15.0 (issue #185).

Each of these replaced an O(regions x pixels) or O(blocks x surfaces) Python
loop with a vectorized equivalent. The point of these tests is not speed — it
is that the fast path still produces what the slow one did, including at the
awkward shapes real maps produce (a 4993-pixel raster is not a multiple of the
32-pixel block size, and water masks arrive as uint8 rather than bool).
"""

import zipfile

import numpy as np
import pytest
from scipy import ndimage

from config.enfusion import (
    BLOCK_FACE_SIZE,
    BLOCK_SURFACE_THRESHOLD,
    MAX_SURFACES_PER_BLOCK,
)


# ---------------------------------------------------------------------------
# Reference implementations — the loops these optimizations replaced
# ---------------------------------------------------------------------------

def _reference_block_saturation(masks, block_size, threshold):
    h, w = next(iter(masks.values())).shape
    violations = 0
    total_blocks = 0
    details = []
    for y in range(0, h, block_size):
        for x in range(0, w, block_size):
            total_blocks += 1
            names = [
                name for name, mask in masks.items()
                if mask[y:y + block_size, x:x + block_size].max() > threshold
            ]
            if len(names) > MAX_SURFACES_PER_BLOCK:
                violations += 1
                details.append({
                    "block_x": x // block_size,
                    "block_y": y // block_size,
                    "surfaces": len(names),
                    "surface_names": names,
                })
    return {"violations": violations, "total_blocks": total_blocks, "details": details}


def _make_masks(size, n_surfaces=8, seed=0):
    rng = np.random.default_rng(seed)
    names = [
        "grass", "forest_floor", "pine_floor", "asphalt",
        "gravel", "crop", "dirt", "rock", "sand", "water_edge",
    ][:n_surfaces]
    masks = {}
    for name in names:
        m = np.zeros((size, size), dtype=np.uint8)
        for _ in range(max(4, size // 20)):
            r = int(rng.integers(2, max(3, size // 5)))
            y = int(rng.integers(r, size - r))
            x = int(rng.integers(r, size - r))
            m[y - r:y + r, x - r:x + r] = int(rng.integers(1, 256))
        masks[name] = m
    return masks


# ---------------------------------------------------------------------------
# Block saturation: vectorized per-block maxima
# ---------------------------------------------------------------------------

class TestBlockSaturation:

    @pytest.mark.parametrize("size", [64, 65, 97, 129, 160, 193])
    def test_matches_the_nested_loop_it_replaced(self, size):
        """Sizes that are and are not a multiple of the 32-px block. A real
        4993-px map ends in a 1-px-wide block column, so the zero padding the
        vectorized version adds must not change any block's maximum."""
        from services.surface_mask_generator import check_block_saturation

        masks = _make_masks(size, seed=size)
        expected = _reference_block_saturation(
            masks, BLOCK_FACE_SIZE, BLOCK_SURFACE_THRESHOLD,
        )
        got = check_block_saturation(masks)

        assert got["total_blocks"] == expected["total_blocks"]
        assert got["violations"] == expected["violations"]
        assert got["details"] == expected["details"]

    def test_partial_edge_block_counts_only_real_pixels(self):
        """A 33x33 mask has a 1x1 block at each edge. Padding it out to 64x64
        must not let the padding hide — or invent — coverage."""
        from services.surface_mask_generator import check_block_saturation

        masks = {}
        for i in range(MAX_SURFACES_PER_BLOCK + 1):
            m = np.zeros((33, 33), dtype=np.uint8)
            m[32, 32] = 255            # only the lone bottom-right block
            masks[f"s{i}"] = m

        result = check_block_saturation(masks)
        assert result["total_blocks"] == 4
        assert result["violations"] == 1
        assert result["details"][0]["block_x"] == 1
        assert result["details"][0]["block_y"] == 1

    def test_empty_masks_are_handled(self):
        from services.surface_mask_generator import check_block_saturation

        result = check_block_saturation({})
        assert result["violations"] == 0
        assert result["total_blocks"] == 0
        assert result["details"] == []


class TestAutoMerge:

    def test_reusing_a_precomputed_scan_gives_the_same_masks(self):
        """generate_surface_masks already ran the scan; passing it in must not
        change the outcome, only skip the second full pass over every mask."""
        from services.surface_mask_generator import (
            auto_merge_violations,
            check_block_saturation,
        )

        masks = _make_masks(160, n_surfaces=9, seed=17)
        assert check_block_saturation(masks)["violations"] > 0, "test needs violations"

        fresh = auto_merge_violations({k: v.copy() for k, v in masks.items()})

        reused_input = {k: v.copy() for k, v in masks.items()}
        saturation = check_block_saturation(reused_input)
        reused = auto_merge_violations(reused_input, saturation=saturation)

        for name in masks:
            assert np.array_equal(fresh[name], reused[name]), name

    def test_merging_reduces_violations(self):
        from services.surface_mask_generator import (
            auto_merge_violations,
            check_block_saturation,
        )

        masks = _make_masks(160, n_surfaces=9, seed=23)
        before = check_block_saturation(masks)["violations"]
        assert before > 0
        merged = auto_merge_violations(masks, default_surface="grass")
        assert check_block_saturation(merged)["violations"] < before


# ---------------------------------------------------------------------------
# Nodata fill: EDT indices instead of a KD-tree over every valid pixel
# ---------------------------------------------------------------------------

class TestNodataFill:

    def test_fill_is_exact_nearest_neighbour(self):
        """Every filled pixel must take the value of a *nearest* valid pixel.
        Checked against a brute-force search, which is what the KD-tree the
        EDT replaced was doing."""
        elevation = np.arange(40 * 40, dtype=np.float32).reshape(40, 40)
        nodata_mask = np.zeros((40, 40), dtype=bool)
        nodata_mask[15:25, 15:25] = True

        filled = elevation.copy()
        nearest = ndimage.distance_transform_edt(
            nodata_mask, return_distances=False, return_indices=True,
        )
        filled[nodata_mask] = filled[
            nearest[0][nodata_mask], nearest[1][nodata_mask]
        ]

        valid_yx = np.argwhere(~nodata_mask)
        for y, x in np.argwhere(nodata_mask):
            d2 = ((valid_yx[:, 0] - y) ** 2 + (valid_yx[:, 1] - x) ** 2)
            best = d2.min()
            candidates = {
                float(elevation[vy, vx])
                for vy, vx in valid_yx[d2 == best]
            }
            assert float(filled[y, x]) in candidates, (
                f"({y},{x}) filled with {filled[y, x]}, not a nearest valid value"
            )

    def test_generate_heightmap_fills_nodata_from_a_geotiff(self):
        """End to end through geotiff_to_array: nodata sentinels must be
        gone and replaced with plausible neighbouring elevations."""
        rasterio = pytest.importorskip("rasterio")
        from rasterio.transform import from_origin
        from rasterio.io import MemoryFile

        from services.heightmap_generator import geotiff_to_array

        data = np.full((64, 64), 250.0, dtype=np.float32)
        data[20:30, 20:30] = -9999.0

        with MemoryFile() as mem:
            with mem.open(
                driver="GTiff", height=64, width=64, count=1,
                dtype="float32", crs="EPSG:4326",
                transform=from_origin(14.0, 63.0, 0.001, 0.001),
                nodata=-9999.0,
            ) as dst:
                dst.write(data, 1)
            raw = mem.read()

        elevation, _ = geotiff_to_array(raw)
        assert not np.any(elevation == -9999.0), "nodata sentinel survived"
        assert np.allclose(elevation, 250.0), "holes not filled from neighbours"


# ---------------------------------------------------------------------------
# Water levelling: grouped by label instead of a mask per region
# ---------------------------------------------------------------------------

class TestWaterLevellingVectorization:

    def test_each_region_gets_its_own_level_and_depth(self):
        """Two lakes at different elevations, carved in one call. The grouped
        implementation must not bleed one region's water level into the other."""
        from services.heightmap_generator import flatten_water_in_heightmap

        elevation = np.full((120, 120), 300.0, dtype=np.float32)
        elevation[10:40, 10:40] = 200.0      # low lake
        elevation[70:110, 70:110] = 400.0    # high lake

        mask = np.zeros((120, 120), dtype=np.uint8)
        mask[10:40, 10:40] = 1
        mask[70:110, 70:110] = 1

        out = flatten_water_in_heightmap(
            elevation, mask, transition_px=0, pixel_size_m=2.0,
            max_depth_m=10.0, shore_slope_m_per_m=0.3,
        )

        low_centre = float(out[25, 25])
        high_centre = float(out[90, 90])
        assert low_centre < 200.0, "low lake not carved below its own surface"
        assert 380.0 < high_centre < 400.0, (
            f"high lake carved to {high_centre}, i.e. from the wrong region's level"
        )

    def test_uint8_and_bool_masks_agree(self):
        """The rasterizers emit uint8; tests historically passed bool. Both
        must carve the same terrain (issue #183 came from that gap)."""
        from services.heightmap_generator import flatten_water_in_heightmap

        rng = np.random.default_rng(4)
        elevation = (rng.random((90, 90)) * 20 + 100).astype(np.float32)
        mask_u8 = np.zeros((90, 90), dtype=np.uint8)
        mask_u8[10:30, 10:30] = 1
        mask_u8[50:80, 40:70] = 1

        a = flatten_water_in_heightmap(elevation, mask_u8, transition_px=2)
        b = flatten_water_in_heightmap(elevation, mask_u8.astype(bool), transition_px=2)
        assert np.array_equal(a, b)

    def test_many_small_regions_are_all_carved(self):
        """The grouped path indexes regions by label; an off-by-one there
        would leave whole ponds untouched."""
        from services.heightmap_generator import flatten_water_in_heightmap

        elevation = np.full((200, 200), 150.0, dtype=np.float32)
        mask = np.zeros((200, 200), dtype=np.uint8)
        centres = [(y, x) for y in range(10, 200, 20) for x in range(10, 200, 20)]
        for y, x in centres:
            mask[y - 4:y + 4, x - 4:x + 4] = 1

        labelled, n = ndimage.label(mask.astype(bool))
        assert n == len(centres)

        out = flatten_water_in_heightmap(
            elevation, mask, transition_px=0, pixel_size_m=2.0,
            max_depth_m=8.0, shore_slope_m_per_m=0.3,
        )
        for y, x in centres:
            assert out[y, x] < 150.0, f"pond at ({y},{x}) was never carved"

    def test_empty_mask_returns_the_input_untouched(self):
        from services.heightmap_generator import flatten_water_in_heightmap

        elevation = np.full((32, 32), 42.0, dtype=np.float32)
        out = flatten_water_in_heightmap(elevation, np.zeros((32, 32), dtype=np.uint8))
        assert np.array_equal(out, elevation)


# ---------------------------------------------------------------------------
# Export ZIP: store what is already compressed
# ---------------------------------------------------------------------------

class TestZipCompression:

    def test_png_is_stored_and_text_is_deflated(self, tmp_path):
        from services.map_generator import _write_zip_archive

        out = tmp_path / "job"
        (out / "sub").mkdir(parents=True)
        (out / "surface_grass.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50_000)
        (out / "sub" / "world.layer").write_text("<layer/>" * 5_000)
        (out / "SETUP_GUIDE.md").write_text("# guide\n" * 2_000)

        zip_path = tmp_path / "out.zip"
        raw = _write_zip_archive(out, zip_path, "MyMap", "job-1")

        assert raw == sum(
            p.stat().st_size for p in out.rglob("*") if p.is_file()
        )
        with zipfile.ZipFile(zip_path) as zf:
            by_name = {i.filename: i for i in zf.infolist()}
            assert by_name["MyMap/surface_grass.png"].compress_type == zipfile.ZIP_STORED
            assert by_name["MyMap/sub/world.layer"].compress_type == zipfile.ZIP_DEFLATED
            assert by_name["MyMap/SETUP_GUIDE.md"].compress_type == zipfile.ZIP_DEFLATED
            # The archive must still be readable and byte-exact.
            assert zf.read("MyMap/SETUP_GUIDE.md") == (out / "SETUP_GUIDE.md").read_bytes()
            assert zf.read("MyMap/surface_grass.png") == (out / "surface_grass.png").read_bytes()

    def test_unknown_extensions_are_still_deflated(self, tmp_path):
        """The store list is a whitelist of known-compressed formats; anything
        new the pipeline starts emitting keeps the safe default."""
        from services.map_generator import _write_zip_archive

        out = tmp_path / "job"
        out.mkdir()
        (out / "thing.brandnew").write_text("a" * 20_000)

        zip_path = tmp_path / "out.zip"
        _write_zip_archive(out, zip_path, "MyMap", "job-2")
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.infolist()[0].compress_type == zipfile.ZIP_DEFLATED


# ---------------------------------------------------------------------------
# Step timing instrumentation
# ---------------------------------------------------------------------------

class TestStepTiming:

    def test_assigning_current_step_closes_the_previous_phase(self):
        from services.map_generator import MapGenerationJob

        job = MapGenerationJob("j1", [], {}, "sess")
        job.current_step = "One..."
        job.current_step = "Two..."
        job.current_step = "Two..."          # repeat must not split the phase
        job.close_timing()

        steps = [t["step"] for t in job.step_timings]
        assert steps == ["One...", "Two..."]
        assert all(t["seconds"] >= 0 for t in job.step_timings)
        assert job.current_step == "Two..."

    def test_timing_summary_ranks_the_slowest_first(self):
        from services.map_generator import MapGenerationJob

        job = MapGenerationJob("j2", [], {}, "sess")
        job.step_timings = [
            {"step": "Fast...", "seconds": 1.0},
            {"step": "Slow...", "seconds": 30.0},
            {"step": "Medium...", "seconds": 5.0},
        ]
        summary = job.timing_summary(top=2)
        assert summary.startswith("Slow 30.0s")
        assert "Medium 5.0s" in summary
        assert "Fast" not in summary

    def test_timings_are_exposed_to_the_frontend(self):
        from services.map_generator import MapGenerationJob

        job = MapGenerationJob("j3", [], {}, "sess")
        job.current_step = "Only step..."
        job.close_timing()
        assert job.to_dict()["step_timings"] == job.step_timings

    def test_close_timing_is_idempotent(self):
        from services.map_generator import MapGenerationJob

        job = MapGenerationJob("j4", [], {}, "sess")
        job.current_step = "One..."
        job.close_timing()
        job.close_timing()
        assert len(job.step_timings) == 1
