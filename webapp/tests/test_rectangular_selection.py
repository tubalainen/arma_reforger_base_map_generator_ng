"""
Rectangular (non-square) terrain selection — issue #197.

Enfusion's TerrainEntity supports non-square terrain: the New Terrain dialog
takes a grid size for X and one for Z, with no ratio restriction. Rectangles
were dropped in v1.5.9 as part of a toolbar tidy (issue #128), not for a
technical reason, and square-only selection makes every oblong region pay for
terrain it never uses — a 20.5 x 12.5 km area forced to a square covers +63%
more ground.

These tests cover the three places that actually assumed a square, all found by
running a real 2:1 rectangle through the pipeline:

  1. ``compute_satellite_target_dims`` capped each axis independently at the
     same value, so any terrain past the cap got a SQUARE texture — a
     4096x2048-face map produced 8192x8192, squashing the imagery 2:1.
  2. ``validate_and_harden_rasters`` asserted the satellite was square.
  3. ``SetupGuideGenerator`` printed X twice, so the guide told the user to
     build a square terrain for a rectangular heightmap.

The frontend's per-axis snapping is exercised directly in node where available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

APP_JS = WEBAPP_DIR / "static" / "js" / "app.js"
INDEX_HTML = WEBAPP_DIR / "static" / "index.html"
STYLE_CSS = WEBAPP_DIR / "static" / "css" / "style.css"

# A rectangle whose axes are both valid tile multiples: the London case from
# issue #197 (20.48 x 12.544 km on a 2 m grid).
FACES_X, FACES_Z = 10240, 6272


def _metadata(dimensions: str, terrain_size: str) -> dict:
    return {
        "heightmap": {
            "dimensions": dimensions,
            "grid_cell_size_m": 2.0,
            "terrain_size_m": terrain_size,
        },
        "elevation": {
            "min_elevation_m": 0.0,
            "max_elevation_m": 100.0,
            "height_scale": 0.03125,
            "dialog_height_scale": 0.03125,
            "height_offset": 0.0,
        },
        "input": {
            "bbox": {"south": 51.4, "north": 51.51, "west": -0.25, "east": 0.04},
            "countries": ["GB"],
        },
    }


# ---------------------------------------------------------------------------
# 1. Satellite texture must keep the terrain's aspect ratio
# ---------------------------------------------------------------------------


class TestSatelliteAspectRatio:
    def test_rectangular_terrain_gets_rectangular_texture(self):
        from services.satellite_service import (
            SATELLITE_MAX_DIM,
            compute_satellite_target_dims,
        )

        x, z = compute_satellite_target_dims(FACES_X + 1, FACES_Z + 1)
        assert max(x, z) == SATELLITE_MAX_DIM
        assert x != z, (
            "a 10240x6272-face terrain must not get a square satellite texture"
        )
        want = (FACES_X + 1) / (FACES_Z + 1)
        assert abs((x / z) - want) / want < 0.02

    def test_the_case_from_issue_50_is_not_squashed(self):
        """4096 x 2048 faces — the rectangle named in issue #50. Before the
        fix this returned 8192x8192, stretching the imagery 2:1 on the
        ground."""
        from services.satellite_service import compute_satellite_target_dims

        x, z = compute_satellite_target_dims(4097, 2049)
        assert abs((x / z) - 2.0) < 0.02, f"got {x}x{z}, expected roughly 2:1"

    def test_square_terrain_still_gets_square_texture(self):
        from services.satellite_service import compute_satellite_target_dims

        x, z = compute_satellite_target_dims(2049, 2049)
        assert x == z


# ---------------------------------------------------------------------------
# 2. Raster contract: aspect, not squareness
# ---------------------------------------------------------------------------


class TestRasterContractAcceptsRectangles:
    def _write_rasters(self, tmp: Path, faces_x: int, faces_z: int, sat: tuple):
        from PIL import Image

        (tmp / "heightmap.asc").write_text(
            f"ncols {faces_x + 1}\nnrows {faces_z + 1}\n"
            "xllcorner 0\nyllcorner 0\ncellsize 2\nNODATA_value -9999\n"
        )
        Image.new("L", (faces_x, faces_z)).save(tmp / "surface_grass.png")
        Image.new("RGB", sat).save(tmp / "satellite_map.png")

    def test_rectangular_rasters_pass(self):
        from services.raster_contract import validate_and_harden_rasters
        from services.satellite_service import compute_satellite_target_dims

        tmp = Path(tempfile.mkdtemp())
        sat = compute_satellite_target_dims(FACES_X + 1, FACES_Z + 1)
        self._write_rasters(tmp, FACES_X, FACES_Z, sat)

        report = validate_and_harden_rasters(tmp, FACES_X, FACES_Z)
        assert report["ok"], f"rectangular terrain rejected: {report['issues']}"

    def test_squashed_satellite_is_still_caught(self):
        """The guard must actually fire — a square texture on a 2:1 terrain is
        the exact defect this check exists for."""
        from services.raster_contract import validate_and_harden_rasters

        tmp = Path(tempfile.mkdtemp())
        self._write_rasters(tmp, FACES_X, FACES_Z, (8192, 8192))

        report = validate_and_harden_rasters(tmp, FACES_X, FACES_Z)
        assert not report["ok"], (
            "an 8192x8192 texture on a 10240x6272 terrain must be reported"
        )
        assert any("aspect" in str(i) for i in report["issues"])

    def test_square_terrain_still_passes(self):
        from services.raster_contract import validate_and_harden_rasters

        tmp = Path(tempfile.mkdtemp())
        self._write_rasters(tmp, 2048, 2048, (8192, 8192))
        report = validate_and_harden_rasters(tmp, 2048, 2048)
        assert report["ok"], report["issues"]


# ---------------------------------------------------------------------------
# 3. SETUP_GUIDE must state both axes
# ---------------------------------------------------------------------------


class TestSetupGuideStatesBothAxes:
    def _guide(self, dimensions: str, terrain_size: str) -> str:
        from services.setup_guide_generator import SetupGuideGenerator

        out = Path(tempfile.mkdtemp())
        SetupGuideGenerator(
            map_name="RectMap", metadata=_metadata(dimensions, terrain_size)
        ).generate(out)
        return (out / "SETUP_GUIDE.md").read_text(encoding="utf-8")

    def test_rectangular_guide_never_prints_a_square_grid(self):
        guide = self._guide("10241x6273", "20480 x 12544 m")
        assert f"{FACES_X}" in guide and f"{FACES_Z}" in guide
        # The pre-fix guide said "10240 x 10240" and "Terrain Grid Size Z: 10240".
        for bad in (
            f"{FACES_X} × {FACES_X}",
            f"{FACES_X}×{FACES_X}",
            f"Terrain Grid Size Z:    {FACES_X}",
        ):
            assert bad not in guide, f"guide still claims a square terrain: {bad!r}"

    def test_rectangular_guide_reports_z_correctly(self):
        guide = self._guide("10241x6273", "20480 x 12544 m")
        assert f"Terrain Grid Size Z:    {FACES_Z}" in guide
        assert f"Terrain Grid Size X:    {FACES_X}" in guide

    def test_rectangular_guide_warns_about_the_sync_button(self):
        """The dialog's width/height sync button is ON by default; a user who
        leaves it on gets a square terrain and a heightmap that will not
        import.

        The tooltip is quoted verbatim (confirmed against Tools 1.8.0.13 on
        issue #197) rather than described from memory — the guide first
        shipped with an invented "`=` control between the fields", which is
        exactly the failure mode docs/ENFUSION_CONTRACT.md warns about. Both
        the expert section and the terrain-creation phase must carry it, so
        count occurrences rather than just testing for presence.
        """
        guide = self._guide("10241x6273", "20480 x 12544 m")
        tooltip = "Synchronize width and height to create square terrain"
        assert guide.count(tooltip) >= 2, (
            f"tooltip quoted {guide.count(tooltip)}x; both the expert section "
            "and Phase 2 must carry it"
        )
        assert "`=` control" not in guide, "the invented control name is back"

    def test_square_guide_is_unchanged_in_substance(self):
        guide = self._guide("2049x2049", "4096 x 4096 m")
        assert "Terrain Grid Size X:    2048" in guide
        assert "Terrain Grid Size Z:    2048" in guide
        assert "turn off the width/height sync button" not in guide.lower(), (
            "a square terrain must not tell the user to unsync anything"
        )


# ---------------------------------------------------------------------------
# 4. The pipeline's own sizing must match what the browser drew
# ---------------------------------------------------------------------------


class TestPipelineGridMatchesTheBrowser:
    """map_generator derives the grid the pipeline actually builds. The browser
    snaps the drawn box to *its* answer, so if the two disagree the user is
    shown one terrain and shipped another (issue #197, reported by OrcVole)."""

    def test_axes_are_derived_independently(self):
        from services.map_generator import derive_terrain_grid

        assert derive_terrain_grid(20500, 12500, 2.0) == (FACES_X, FACES_Z)

    def test_the_metre_to_face_division_is_not_pre_rounded(self):
        """383 m is 191.5 faces. Pre-rounding gives 192 = exactly 1.5 tiles,
        which rounds up to 2 tiles; one step gives 1.496 tiles -> 1."""
        from services.map_generator import derive_terrain_grid

        assert derive_terrain_grid(383, 383, 2.0) == (128, 128)

    def test_exact_half_tiles_round_up_not_to_even(self):
        from services.map_generator import derive_terrain_grid

        # 640 m = 2.5 tiles; banker's rounding sent this DOWN to 512 m.
        assert derive_terrain_grid(640, 640, 2.0) == (384, 384)

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_agrees_with_the_real_browser_code_across_the_range(self):
        """Run the shipped deriveAxis() in node over 100 m - 40 km and compare
        against the pipeline. This runs the actual app.js source, not a port,
        so the two cannot drift apart unnoticed."""
        from services.map_generator import derive_terrain_grid

        harness = _extract_js_sizing() + """
const out = [];
for (let m = 100; m <= 40000; m++) out.push(deriveAxis(m).N);
console.log(JSON.stringify(out));
"""
        tmp = Path(tempfile.mkdtemp()) / "sweep.mjs"
        tmp.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(tmp)], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stderr
        js = json.loads(proc.stdout)

        bad = [
            m
            for i, m in enumerate(range(100, 40_001))
            if derive_terrain_grid(m, m, 2.0)[0] != js[i]
        ]
        assert not bad, (
            f"{len(bad)} sizes disagree with the browser, e.g. "
            + ", ".join(
                f"{m} m -> pipeline {derive_terrain_grid(m, m, 2.0)[0]} vs "
                f"browser {js[m - 100]}"
                for m in bad[:3]
            )
        )


# ---------------------------------------------------------------------------
# 5. Frontend per-axis snapping and the per-axis maximum
# ---------------------------------------------------------------------------


def _extract_js_sizing() -> str:
    """Pull the constants + deriveAxis out of app.js so node can run them.

    deriveAxis depends on nothing but the three constants, which is what makes
    this safe to slice out. If the slice ever stops matching, the test fails
    loudly rather than silently checking nothing.
    """
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("const GRID_CELL_SIZE_M")
    end = src.index("// Derive both terrain axes")
    chunk = src[start:end]
    assert "function deriveAxis(" in chunk, "deriveAxis not found in the slice"
    return chunk


JS_HARNESS = """
const cases = [
  [20500, 12500],   // issue #197's London region
  [4096, 4096],     // square
  [999999, 500],    // absurdly wide - must clamp on X only
  [500, 999999],    // absurdly tall - must clamp on Z only
  [10, 10],         // tiny - must floor at one tile
];
console.log(JSON.stringify(cases.map(([w, h]) => {
  const x = deriveAxis(w), z = deriveAxis(h);
  return { w, h, nx: x.N, nz: z.N, mx: x.m, mz: z.m };
})));
"""


class TestFrontendPerAxisSizing:
    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_axes_snap_independently_and_clamp_per_axis(self):
        tmp = Path(tempfile.mkdtemp()) / "harness.mjs"
        tmp.write_text(_extract_js_sizing() + JS_HARNESS, encoding="utf-8")
        out = subprocess.run(
            ["node", str(tmp)], capture_output=True, text=True, timeout=30
        )
        assert out.returncode == 0, out.stderr
        got = {(c["w"], c["h"]): c for c in json.loads(out.stdout)}

        # Each axis is derived from its own length, not the longer one.
        london = got[(20500, 12500)]
        assert (london["nx"], london["nz"]) == (FACES_X, FACES_Z)

        # A square still resolves to equal axes.
        square = got[(4096, 4096)]
        assert square["nx"] == square["nz"] == 2048

        # The maximum is per axis: the oversized axis clamps, the other does
        # not get dragged up with it.
        wide = got[(999999, 500)]
        assert wide["nx"] == 16384 and wide["mx"] == 32768
        assert wide["nz"] == 256

        tall = got[(500, 999999)]
        assert tall["nz"] == 16384 and tall["nx"] == 256

        # Never below one tile.
        assert got[(10, 10)]["nx"] == 128

    def test_rectangle_tool_is_wired_into_the_toolbar(self):
        js = APP_JS.read_text(encoding="utf-8")
        assert "L.Draw.Rect = L.Draw.Rectangle.extend(" in js
        assert "leaflet-draw-draw-rect" in js
        assert "new L.Draw.Rect(map" in js
        # CREATED must accept both shapes, and remember which was drawn.
        assert "event.layerType !== 'rect'" in js
        assert "_selectionShape" in js

    def test_square_tool_is_still_there(self):
        js = APP_JS.read_text(encoding="utf-8")
        assert "L.Draw.Square = L.Draw.Rectangle.extend(" in js
        assert "new L.Draw.Square(map" in js
        assert "L.Edit.Square" in js, "square must keep its 1:1 edit handler"

    def test_rectangle_button_has_an_icon(self):
        assert "leaflet-draw-draw-rect" in STYLE_CSS.read_text(encoding="utf-8")

    def test_sidebar_mentions_both_tools(self):
        html = INDEX_HTML.read_text(encoding="utf-8").lower()
        assert "rectangle" in html and "square" in html
