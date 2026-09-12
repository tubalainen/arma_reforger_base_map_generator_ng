"""
Terrain geometry: one projected rectangle, no per-axis scaling — issue #203.

Two faults, one cause. The terrain used to be defined by projecting *two*
corners of the drawn WGS84 box and then squeezing that extent into the terrain
box with an independent scale per axis:

  1. **Anisotropy.** On the Hammarö map the scales were x0.97387 in X against
     x1.01954 in Z — 4.69% apart, so a circle on the ground became a 4.7%
     ellipse in game and a 1 km measurement was ~25 m out one way, ~20 m the
     other.
  2. **Lost ground.** A lat/lon box is a sheared quadrilateral in a projected
     CRS, so NE-minus-SW describes a rectangle that two of the four corners
     fall outside. The NW corner landed at local z=6401 m in a terrain only
     6277 m deep: 247 m of what the user drew was never in the terrain.

Deriving the grid from the projected extent does not fix (1) on its own —
each axis still snaps to a 128-face tile, which puts its scale back up to
128 m / extent away from 1.0, and the axes round independently. Measured worst
case on a 6 km map is ~4.3%; Hammarö happened to land at 0.14%. Removing the
scale is what makes it structurally zero.

So: the terrain is ``terrain_size_m`` metres of the projected CRS, centred on
the projected extent of the drawn area, and local coordinates are a pure
translation. Snapping becomes margin instead of distortion.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

pytest.importorskip("pyproj")

# The real Hammarö selection, the map the issue was measured on.
BBOX = {
    "west": 13.490100958192622, "south": 59.250892869538426,
    "east": 13.589137933408944, "north": 59.30838478473785,
}
CRS = "EPSG:3006"


def _sized_transformer():
    from config.terrain import DEFAULT_GRID_CELL_SIZE
    from services.coordinate_transformer import CoordinateTransformer
    from services.map_generator import derive_terrain_grid_projected

    t = CoordinateTransformer(bbox=BBOX, crs=CRS)
    fx, fz = derive_terrain_grid_projected(t, DEFAULT_GRID_CELL_SIZE)
    t.set_terrain_size((fx * DEFAULT_GRID_CELL_SIZE, fz * DEFAULT_GRID_CELL_SIZE))
    return t, fx, fz


class TestNoAnisotropy:
    def test_both_axes_have_the_same_scale(self):
        """1 km on the ground must be 1 km in game on *both* axes. This is the
        whole of fault 1 — before the fix the two differed by 4.69%."""
        from pyproj import Geod

        t, fx, fz = _sized_transformer()
        g = Geod(ellps="WGS84")
        clon = (BBOX["west"] + BBOX["east"]) / 2
        clat = (BBOX["south"] + BBOX["north"]) / 2
        x0, z0 = t.wgs84_to_local(clon, clat)

        scales = []
        for azimuth in (90, 0):  # east, north
            lon2, lat2, _ = g.fwd(clon, clat, azimuth, 1000.0)
            x1, z1 = t.wgs84_to_local(lon2, lat2)
            scales.append(math.hypot(x1 - x0, z1 - z0) / 1000.0)

        anisotropy = max(scales) / min(scales) - 1
        assert anisotropy < 0.001, (
            f"axes scale differently by {anisotropy:.3%} "
            f"(east {scales[0]:.5f}, north {scales[1]:.5f})"
        )

    def test_the_scale_is_the_projections_own(self):
        """Not 1.000000 — a projected CRS has its own scale factor, and
        honouring it is the point. Just make sure it is small and shared."""
        from pyproj import Geod

        t, _, _ = _sized_transformer()
        g = Geod(ellps="WGS84")
        clon = (BBOX["west"] + BBOX["east"]) / 2
        clat = (BBOX["south"] + BBOX["north"]) / 2
        x0, z0 = t.wgs84_to_local(clon, clat)
        lon2, lat2, _ = g.fwd(clon, clat, 90, 1000.0)
        x1, z1 = t.wgs84_to_local(lon2, lat2)
        assert 0.99 < math.hypot(x1 - x0, z1 - z0) / 1000.0 < 1.01


class TestNoGroundIsLost:
    def test_all_four_drawn_corners_are_inside_the_terrain(self):
        """Fault 2. The NW corner used to land 124 m past the north edge."""
        t, fx, fz = _sized_transformer()
        w, d = t.terrain_size_m
        for name, (lon, lat) in {
            "SW": (BBOX["west"], BBOX["south"]),
            "SE": (BBOX["east"], BBOX["south"]),
            "NE": (BBOX["east"], BBOX["north"]),
            "NW": (BBOX["west"], BBOX["north"]),
        }.items():
            x, z = t.wgs84_to_local(lon, lat)
            assert 0 <= x <= w and 0 <= z <= d, (
                f"{name} corner at ({x:.1f}, {z:.1f}) is outside the "
                f"{w}x{d} m terrain"
            )

    def test_the_extent_comes_from_the_whole_perimeter(self):
        """Two corners under-measure a sheared quadrilateral. On this bbox the
        four-corner depth is ~247 m greater than NE-minus-SW."""
        from pyproj import Transformer

        from services.coordinate_transformer import CoordinateTransformer

        t = CoordinateTransformer(bbox=BBOX, crs=CRS)
        tr = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
        sw = tr.transform(BBOX["west"], BBOX["south"])
        ne = tr.transform(BBOX["east"], BBOX["north"])
        two_corner_depth = ne[1] - sw[1]
        assert t.projected_depth > two_corner_depth + 100, (
            "extent still looks like a two-corner measurement"
        )

    def test_the_grid_rounds_up_so_the_terrain_contains_the_area(self):
        t, fx, fz = _sized_transformer()
        assert fx * 2 >= t.projected_width
        assert fz * 2 >= t.projected_depth


class TestRoundTrip:
    def test_local_to_wgs84_inverts_wgs84_to_local(self):
        t, _, _ = _sized_transformer()
        for lon, lat in (
            (BBOX["west"] + 0.01, BBOX["south"] + 0.01),
            ((BBOX["west"] + BBOX["east"]) / 2, (BBOX["south"] + BBOX["north"]) / 2),
            (BBOX["east"] - 0.01, BBOX["north"] - 0.01),
        ):
            x, z = t.wgs84_to_local(lon, lat)
            lon2, lat2 = t.local_to_wgs84(x, z)
            assert abs(lon2 - lon) < 1e-7 and abs(lat2 - lat) < 1e-7


class TestEveryLayerSharesOneRectangle:
    """The heightmap, the masks and the splines must resolve against the same
    rectangle. Before #203 each derived its own and they agreed only because
    everything was scaled to fit."""

    def _marker(self, lon, lat, d=0.0015):
        return {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[
                [lon - d, lat - d], [lon + d, lat - d],
                [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
            ]]}}]}

    def test_heightmap_and_mask_frames_agree(self):
        import numpy as np

        from services.heightmap_generator import project_features_to_terrain
        from services.utils.rasterize import rasterize_features_to_mask

        t, fx, fz = _sized_transformer()
        bounds = t.terrain_bounds_projected
        proj = project_features_to_terrain(
            self._marker(BBOX["west"] + 0.004, BBOX["north"] - 0.004), t
        )

        def centroid(w, h):
            m = rasterize_features_to_mask(proj, w, h, bounds)
            ys, xs = np.where(m > 0)
            assert len(xs), "marker did not rasterise"
            return xs.mean() / w, ys.mean() / h

        hx, hy = centroid(fx + 1, fz + 1)   # heightmap: vertex grid
        mx, my = centroid(fx, fz)           # masks: face grid
        dx, dz = (hx - mx) * fx * 2, (hy - my) * fz * 2
        assert abs(dx) < 3 and abs(dz) < 3, (
            f"heightmap and mask frames are {dx:.2f} m / {dz:.2f} m apart "
            f"(one face is 2 m)"
        )

    def test_the_dem_warp_lands_where_the_vectors_do(self):
        """The DEM is warped into the terrain rectangle rather than array-
        resized, so elevation and vectors describe the same ground."""
        import numpy as np
        from rasterio.transform import from_bounds

        from services.heightmap_generator import warp_dem_to_terrain

        t, fx, fz = _sized_transformer()
        W = H = 1200
        dem = np.full((H, W), 100.0, dtype="float32")
        row, col = 300, 400
        dem[row - 1:row + 2, col - 1:col + 2] = 900.0
        lon = BBOX["west"] + (col + 0.5) * (BBOX["east"] - BBOX["west"]) / W
        lat = BBOX["north"] - (row + 0.5) * (BBOX["north"] - BBOX["south"]) / H
        meta = {
            "crs": "EPSG:4326",
            "bounds": (BBOX["west"], BBOX["south"], BBOX["east"], BBOX["north"]),
            "transform": from_bounds(
                BBOX["west"], BBOX["south"], BBOX["east"], BBOX["north"], W, H
            ),
        }

        out, _ = warp_dem_to_terrain(dem, meta, t, (fx + 1, fz + 1))
        bright = out > out.max() * 0.6
        ys, xs = np.where(bright)
        wts = out[bright] - 100
        got_x = (xs * wts).sum() / wts.sum() / (out.shape[1] - 1) * (fx * 2)
        got_z = (1 - (ys * wts).sum() / wts.sum() / (out.shape[0] - 1)) * (fz * 2)
        exp_x, exp_z = t.wgs84_to_local(lon, lat)
        assert abs(got_x - exp_x) < 5 and abs(got_z - exp_z) < 5, (
            f"DEM peak at ({got_x:.1f}, {got_z:.1f}), vectors say "
            f"({exp_x:.1f}, {exp_z:.1f})"
        )


class TestBrowserAndPipelineAgree:
    """The sidebar must show the grid the pipeline will build. The browser
    cannot compute it — it needs the projected extent, which depends on the
    CRS — so the server returns it (issues #197, #203)."""

    def test_the_endpoint_returns_the_pipelines_own_grid(self):
        from config.terrain import DEFAULT_GRID_CELL_SIZE
        from main import _derive_terrain_preview
        from services.map_generator import derive_terrain_grid_projected

        polygon = [
            [BBOX["west"], BBOX["south"]], [BBOX["east"], BBOX["south"]],
            [BBOX["east"], BBOX["north"]], [BBOX["west"], BBOX["north"]],
            [BBOX["west"], BBOX["south"]],
        ]
        preview = _derive_terrain_preview(polygon, CRS)
        t, fx, fz = _sized_transformer()
        assert (preview["faces_x"], preview["faces_z"]) == (fx, fz)
        assert preview["size_x_m"] == fx * DEFAULT_GRID_CELL_SIZE
        assert preview["heightmap_px_z"] == fz + 1

    def test_the_frontend_prefers_the_server_value(self):
        js = (WEBAPP_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
        # It must actually take the server's value, not merely mention it.
        assert "authoritativeTerrain = data.terrain" in js
        # and drop it when the shape changes, or it shows a stale grid.
        assert js.count("authoritativeTerrain = null") >= 2
        # and the readout must prefer it over the local estimate.
        assert "const a = authoritativeTerrain" in js
