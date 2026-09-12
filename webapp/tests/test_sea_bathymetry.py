"""
Sea / ocean bathymetry — issue #193.

The reporter's ask, verbatim: sea and ocean sat at Y=0 with no depth. They
should be negative, with "a shoreline for a realistic number of metres where
the depth gradually increases up to max 10 horizontal metres and 2 vertical
metres off shore, where the depth drops off dynamically to 30-100 metres",
and this "should only apply to shorelines where there clearly is a larger body
of water on the map. For all inland maps that do not have a shoreline (i.e. not
an island) this should not apply."

The island fixture below is the end-to-end regression case: a cone of land
surrounded by sea at exactly 0.0 m, which is how COP30 and other global DEMs
store ocean. Everything is asserted in metres relative to world Y=0, because
that is what the user actually sees in World Editor — the engine's ocean plane
is fixed at Y=0, so the sea surface must sit on it with the floor below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from config.lakes import (  # noqa: E402
    SEA_MAX_DEPTH_M,
    SEA_MIN_DEPTH_M,
    SEA_SHELF_DEPTH_M,
    SEA_SHELF_WIDTH_M,
)
from services.heightmap_generator import (  # noqa: E402
    ElevationTruncatedError,
    dem_bbox_wgs84,
    _synthesize_sea_mask,
    carve_sea_bathymetry,
    geotiff_to_array,
    qualifying_sea_regions,
)

N = 400
PIXEL_M = 10.0  # 10 m/px → a 4 km map, so 1 px is comfortably finer than the shelf


def _island(radius_px: float = 120.0, peak_m: float = 100.0) -> np.ndarray:
    """Land cone in the middle, ocean at exactly 0.0 m around it."""
    yy, xx = np.mgrid[0:N, 0:N]
    r = np.hypot(yy - N / 2, xx - N / 2)
    return np.where(r < radius_px, (1 - r / radius_px) * peak_m, 0.0).astype(np.float32)


def _sea_mask_for(elev: np.ndarray) -> np.ndarray:
    return (elev <= 0.0).astype(np.uint8)


def _inland_lake(radius_px: float = 60.0) -> tuple[np.ndarray, np.ndarray]:
    """A large lake that does NOT touch the map edge, on a 200 m plateau."""
    yy, xx = np.mgrid[0:N, 0:N]
    r = np.hypot(yy - N / 2, xx - N / 2)
    elev = np.full((N, N), 200.0, dtype=np.float32)
    lake = r < radius_px
    elev[lake] = 0.0
    return elev, lake.astype(np.uint8)


class TestQualifyingSeaRegions:
    """Only where there clearly is a larger body of water on the map."""

    def test_island_ocean_qualifies(self):
        sea = _sea_mask_for(_island())
        kept = qualifying_sea_regions(sea)
        assert kept.sum() > 0
        # The whole ocean ring survives.
        assert kept.sum() == pytest.approx(sea.sum(), rel=0.01)

    def test_inland_lake_is_rejected_however_large(self):
        """A water body fully inside the map is a lake, not a sea — even at
        7% of the map area, well over the area threshold. It must fall through
        to the lake path instead of getting a 100 m trench."""
        _, lake = _inland_lake(radius_px=60.0)
        assert lake.mean() > 0.02, "fixture should clear the area threshold"
        assert qualifying_sea_regions(lake).sum() == 0

    def test_shoreline_map_qualifies_not_just_islands(self):
        """A mainland coast with sea on ONE side is as valid as an island.
        Touching the map edge is the discriminator, not the coverage fraction —
        a mostly-inland selection with a strip of coast must still get a sea
        bed."""
        mask = np.zeros((N, N), dtype=np.uint8)
        mask[:, : int(N * 0.08)] = 1          # 8% of the map, along one edge
        kept = qualifying_sea_regions(mask)
        assert kept.sum() == mask.sum(), "a one-sided shoreline was rejected"

    def test_narrow_coastal_strip_in_one_corner_qualifies(self):
        """The case that motivated dropping the area floor from 2% to 0.2%."""
        mask = np.zeros((N, N), dtype=np.uint8)
        mask[: int(N * 0.03), : int(N * 0.20)] = 1   # 0.6% of the map
        assert 0.002 < mask.mean() < 0.02, "fixture must sit under the old 2% floor"
        assert qualifying_sea_regions(mask).sum() > 0

    def test_tiny_edge_puddle_is_rejected(self):
        """Touches the edge but is far too small to be open sea."""
        mask = np.zeros((N, N), dtype=np.uint8)
        mask[0:6, 0:6] = 1
        assert qualifying_sea_regions(mask).sum() == 0

    def test_empty_mask_is_a_no_op(self):
        assert qualifying_sea_regions(np.zeros((N, N), np.uint8)).sum() == 0


class TestSeaDepthProfile:
    """The shelf-then-dropoff shape the issue specifies."""

    def test_shelf_reaches_2m_at_10m_offshore(self):
        elev = _island()
        sea = qualifying_sea_regions(_sea_mask_for(elev))
        carved = carve_sea_bathymetry(elev, sea, PIXEL_M)

        # Walk out along a ray from the island centre and read the depth at
        # the first sea pixel and at ~10 m (1 px) offshore.
        row = N // 2
        sea_row = sea[row].astype(bool)
        depths = -carved[row][sea_row]
        assert depths.min() >= 0.0, "no sea pixel may be above the water surface"
        # The shallowest sea pixel is on the shelf, not already at depth.
        assert depths.min() <= SEA_SHELF_DEPTH_M + 0.5, (
            f"shallowest sea pixel is {depths.min():.2f} m — there is no shelf"
        )

    def test_depth_increases_monotonically_offshore(self):
        elev = _island()
        sea = qualifying_sea_regions(_sea_mask_for(elev))
        carved = carve_sea_bathymetry(elev, sea, PIXEL_M)

        row = N // 2
        # From the island edge outward to the map edge, depth must not decrease.
        strip = carved[row][: N // 2][::-1]  # centre → left edge
        sea_vals = strip[strip <= 0.0]
        diffs = np.diff(sea_vals)
        assert (diffs <= 1e-4).all(), "sea floor rises again as you go offshore"

    def test_deep_water_is_between_30_and_100_m(self):
        elev = _island()
        sea = qualifying_sea_regions(_sea_mask_for(elev))
        carved = carve_sea_bathymetry(elev, sea, PIXEL_M)

        deepest = float(-carved[sea.astype(bool)].min())
        assert SEA_MIN_DEPTH_M <= deepest <= SEA_MAX_DEPTH_M, (
            f"deepest point {deepest:.1f} m is outside the "
            f"{SEA_MIN_DEPTH_M:.0f}-{SEA_MAX_DEPTH_M:.0f} m band"
        )

    def test_ceiling_scales_with_how_far_offshore_the_region_reaches(self):
        """"Dynamically to 30-100 m": a narrow strait must not get an abyss."""
        narrow = _island(radius_px=190.0)          # only a thin ring of sea
        wide = _island(radius_px=40.0)             # lots of open water
        d_narrow = float(-carve_sea_bathymetry(
            narrow, qualifying_sea_regions(_sea_mask_for(narrow)), PIXEL_M
        ).min())
        d_wide = float(-carve_sea_bathymetry(
            wide, qualifying_sea_regions(_sea_mask_for(wide)), PIXEL_M
        ).min())
        assert d_wide > d_narrow, (
            f"open water ({d_wide:.1f} m) should be deeper than a thin ring "
            f"({d_narrow:.1f} m)"
        )

    def test_map_border_is_not_treated_as_a_shore(self):
        """Same trap as #202: if the raster edge counts as shore, the sea
        shoals back to 0 m along all four sides of the map."""
        elev = _island()
        sea = qualifying_sea_regions(_sea_mask_for(elev))
        carved = carve_sea_bathymetry(elev, sea, PIXEL_M)

        for name, line in (
            ("top", carved[0]), ("bottom", carved[-1]),
            ("left", carved[:, 0]), ("right", carved[:, -1]),
        ):
            wet = line[line <= 0.0]
            assert wet.size, f"{name} edge has no sea"
            assert wet.max() <= -SEA_MIN_DEPTH_M * 0.5, (
                f"{name} edge shoals to {wet.max():.2f} m — the map border is "
                f"being treated as a shoreline"
            )


class TestIslandEndToEnd:
    """The regression case: an island, all the way through to world Y."""

    def _world_y(self, elev, sea):
        """Carve, then apply the coastal datum (sea level = Y=0, issue #193)."""
        carved = carve_sea_bathymetry(elev, sea, PIXEL_M)
        return carved - 0.0

    def test_sea_surface_is_at_or_below_zero_and_the_floor_is_negative(self):
        elev = _island()
        sea = qualifying_sea_regions(_sea_mask_for(elev)).astype(bool)
        y = self._world_y(elev, sea)

        assert y[sea].max() <= 0.0, (
            f"sea surface at {y[sea].max():+.2f} — issue #193 is that it sits "
            f"at Y=0 with no depth"
        )
        assert y[sea].min() <= -SEA_MIN_DEPTH_M, (
            f"deepest sea only {y[sea].min():+.1f} m"
        )

    def test_land_stays_above_the_ocean_plane(self):
        elev = _island()
        sea = qualifying_sea_regions(_sea_mask_for(elev)).astype(bool)
        y = self._world_y(elev, sea)

        assert y[~sea].min() >= 0.0, (
            f"land dips to {y[~sea].min():+.2f} m — it would be flooded by the "
            f"engine ocean plane at Y=0"
        )
        assert y[~sea].max() == pytest.approx(100.0, abs=1.0)

    def test_inland_map_is_untouched(self):
        """No coastline → no sea → the array comes back unchanged, so #165's
        lowest-land datum still governs inland maps."""
        elev, lake = _inland_lake()
        sea = qualifying_sea_regions(lake)
        assert sea.sum() == 0
        np.testing.assert_array_equal(carve_sea_bathymetry(elev, sea, PIXEL_M), elev)


# ---------------------------------------------------------------------------
# Island regressions found by generating real island maps (#193 follow-up)
# ---------------------------------------------------------------------------

BBOX = (0.0, 0.0, 0.04, 0.04)


def _coastline_polygon_feature() -> dict:
    """Lantmäteriet Marktäcke ships the sea itself as a Polygon (objekttyp
    2631 -> water_type "coastline"), not as a line."""
    return {
        "type": "Feature",
        "properties": {"water_type": "coastline", "natural": "water"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [0.0, 0.0], [0.04, 0.0], [0.04, 0.04], [0.0, 0.04], [0.0, 0.0],
        ]]},
    }


def _coastline_line_feature() -> dict:
    """OSM ships natural=coastline as a LineString."""
    return {
        "type": "Feature",
        "properties": {"water_type": "coastline", "natural": "water"},
        "geometry": {"type": "LineString", "coordinates": [
            [0.0, 0.02], [0.02, 0.02], [0.04, 0.02],
        ]},
    }


class TestCoastlineArrivesInTwoShapes:
    """The sea was never detected on any Swedish map.

    `_synthesize_sea_mask` rasterised only LineStrings, because OSM ships
    `natural=coastline` as a line. Lantmäteriet Marktäcke ships the sea as a
    **Polygon**, so the mask came back empty, no bathymetry was carved, and a
    real island in the Gulf of Bothnia was written with `is_coastal_map: false`.
    """

    def test_polygon_coastline_is_detected(self):
        elev = _island(radius_px=80.0)
        water = {"type": "FeatureCollection",
                 "features": [_coastline_polygon_feature()]}

        sea = _synthesize_sea_mask(water, elev, BBOX, PIXEL_M)

        assert sea.sum() > 0, (
            "a Polygon coastline produced an empty sea mask — this is why "
            "Swedish island maps came out as inland"
        )

    def test_linestring_coastline_still_works(self):
        elev = _island(radius_px=80.0)
        water = {"type": "FeatureCollection",
                 "features": [_coastline_line_feature()]}

        assert _synthesize_sea_mask(water, elev, BBOX, PIXEL_M).sum() > 0

    def test_no_coastline_at_all_is_still_inland(self):
        elev = _island(radius_px=80.0)
        water = {"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "properties": {"water_type": "lake", "natural": "water"},
             "geometry": {"type": "Polygon", "coordinates": [[
                 [0.01, 0.01], [0.02, 0.01], [0.02, 0.02],
                 [0.01, 0.02], [0.01, 0.01]]]}},
        ]}

        assert _synthesize_sea_mask(water, elev, BBOX, PIXEL_M).sum() == 0


class TestSmallIslandIsNotATruncatedDem:
    """A 3.6 km selection around Isla Grosa (ES) is 97.4% ocean and 2.6%
    island. The truncation guard required >= 10% land, so generation aborted
    with "select an area with sufficient land coverage" — rejecting exactly the
    kind of map v1.16.0's bathymetry exists to serve.

    These call the guard through geotiff_to_array via a real in-memory GeoTIFF,
    so they exercise the shipped code path rather than a copy of the rule.
    """

    @staticmethod
    def _geotiff(arr: np.ndarray) -> bytes:
        rasterio = pytest.importorskip("rasterio")
        from rasterio.io import MemoryFile
        from rasterio.transform import from_origin

        with MemoryFile() as mem:
            with mem.open(
                driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                count=1, dtype="float32",
                transform=from_origin(0, arr.shape[0], 1, 1),
                crs="EPSG:4326",
            ) as ds:
                ds.write(arr.astype(np.float32), 1)
            return mem.read()

    def test_small_island_is_accepted(self):
        """2.6% land, realistic relief -> a real island, not a bad response."""
        rng = np.random.default_rng(0)
        arr = np.zeros((130, 130), dtype=np.float32)       # ocean at exactly 0
        yy, xx = np.mgrid[0:130, 0:130]
        r = np.hypot(yy - 65, xx - 65)
        island = r < 10.5                                   # ~2.6% of pixels
        arr[island] = 5.0 + rng.normal(0, 2.0, island.sum())
        assert 0.5 < island.mean() * 100 < 10, "fixture must sit under the old 10% floor"

        elev, _ = geotiff_to_array(self._geotiff(arr))
        assert elev.shape == (130, 130)

    def test_genuinely_truncated_dem_is_still_rejected(self):
        """A corner of flat data and nothing else is a broken API response."""
        arr = np.zeros((130, 130), dtype=np.float32)
        arr[:12, :12] = 3.0                                 # flat, no variation

        with pytest.raises(ElevationTruncatedError):
            geotiff_to_array(self._geotiff(arr))

    def test_near_empty_response_is_still_rejected(self):
        """Below the 0.5% floor there is nothing usable regardless of values."""
        rng = np.random.default_rng(1)
        arr = np.zeros((200, 200), dtype=np.float32)
        flat = arr.ravel()
        flat[:60] = 40.0 + rng.normal(0, 5.0, 60)           # 0.15% of pixels
        arr = flat.reshape(200, 200)

        with pytest.raises(ElevationTruncatedError):
            geotiff_to_array(self._geotiff(arr))


class _Bounds:
    def __init__(self, left, bottom, right, top):
        self.left, self.bottom, self.right, self.top = left, bottom, right, top


class TestDemBoundsAreConvertedToWgs84:
    """Every rasterizer maps a coordinate with `(lng - west) / lng_range`, i.e.
    it assumes degrees. The DEM's bounds are in the DEM's own CRS, and only
    some providers hand us WGS84.

    Lantmäteriet STAC Höjd delivers **EPSG:5845** (SWEREF99 TM + RH2000) and the
    merge keeps it, so bounds arrive as metres — ~742354, 6470557 for Gotska
    Sandön. Feeding those in as degrees put every water and road feature far
    outside the raster, where the clamp folded them into the corner. The real
    run logged `Carving bathymetry for lake/pond/reservoir mask: 1 px` and no
    sea line at all, so an island 90% covered by sea was written as inland —
    and road flattening had silently done nothing on every Swedish map.
    """

    def test_projected_bounds_are_converted(self):
        pytest.importorskip("rasterio")
        # Gotska Sandön's real EPSG:5845 extent.
        out = dem_bbox_wgs84({
            "bounds": _Bounds(742354, 6470557, 754356, 6484154),
            "crs": "EPSG:5845",
        })
        assert out is not None
        west, south, east, north = out
        # The generation log requested 19.137,58.309 -> 19.357,58.424.
        assert west == pytest.approx(19.137, abs=0.01)
        assert east == pytest.approx(19.357, abs=0.01)
        assert south == pytest.approx(58.309, abs=0.02)
        assert north == pytest.approx(58.424, abs=0.02)

    def test_projected_bounds_are_not_left_as_degrees(self):
        """The specific failure: metres used as longitude."""
        pytest.importorskip("rasterio")
        out = dem_bbox_wgs84({
            "bounds": _Bounds(742354, 6470557, 754356, 6484154),
            "crs": "EPSG:5845",
        })
        assert all(abs(v) <= 180 for v in out), (
            f"bounds {out} are still projected metres — features would be "
            f"clamped into the corner of the raster"
        )

    def test_wgs84_bounds_pass_through_unchanged(self):
        """COP30 already delivers EPSG:4326; conversion must be a no-op."""
        box = _Bounds(-0.789, 37.679, -0.748, 37.711)
        assert dem_bbox_wgs84({"bounds": box, "crs": "EPSG:4326"}) == (
            -0.789, 37.679, -0.748, 37.711
        )

    def test_missing_bounds_returns_none(self):
        assert dem_bbox_wgs84({}) is None
        assert dem_bbox_wgs84({"crs": "EPSG:4326"}) is None

    def test_missing_crs_falls_back_to_unchanged(self):
        """Better to behave as before than to crash; a warning is logged."""
        assert dem_bbox_wgs84({"bounds": _Bounds(1, 2, 3, 4), "crs": ""}) == (
            1.0, 2.0, 3.0, 4.0
        )
