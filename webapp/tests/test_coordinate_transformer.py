"""
Tests for CoordinateTransformer — specifically the WGS84 envelope helper used
by the satellite pipeline to fix issue #58.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

pytest.importorskip("pyproj", reason="pyproj is required for projected CRS tests")

from services.coordinate_transformer import CoordinateTransformer  # noqa: E402


def _bbox(west, south, east, north):
    return {"west": west, "south": south, "east": east, "north": north}


class TestWgs84EnvelopeOfProjectedExtent:
    def test_envelope_contains_user_bbox_for_sweden(self):
        bbox = _bbox(15.0, 58.0, 16.0, 58.5)
        t = CoordinateTransformer(bbox=bbox, crs="EPSG:3006")
        assert t._use_pyproj

        env_w, env_s, env_e, env_n = t.wgs84_envelope_of_projected_extent()

        # The envelope must be at least as large as the user's WGS84 bbox: the
        # projected rectangle's WGS84 footprint extends outward from the
        # user-selected lat/lon corners due to grid convergence.
        assert env_w <= bbox["west"] + 1e-9
        assert env_s <= bbox["south"] + 1e-9
        assert env_e >= bbox["east"] - 1e-9
        assert env_n >= bbox["north"] - 1e-9

        # Sanity: not absurdly large — within a few percent of original span.
        lon_span_orig = bbox["east"] - bbox["west"]
        lat_span_orig = bbox["north"] - bbox["south"]
        assert (env_e - env_w) < lon_span_orig * 1.05
        assert (env_n - env_s) < lat_span_orig * 1.05

    def test_envelope_covers_projected_rectangle_corners(self):
        """The unprojected corners of the projected rectangle must lie inside the envelope."""
        from pyproj import Transformer

        bbox = _bbox(15.0, 58.0, 16.0, 58.5)
        t = CoordinateTransformer(bbox=bbox, crs="EPSG:3006")

        sw_x, sw_y = t._sw_projected
        ne_x, ne_y = t._ne_projected
        env_w, env_s, env_e, env_n = t.wgs84_envelope_of_projected_extent()

        to_wgs84 = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True)
        corners = [(sw_x, sw_y), (ne_x, sw_y), (ne_x, ne_y), (sw_x, ne_y)]
        for px, py in corners:
            lon, lat = to_wgs84.transform(px, py)
            assert env_w - 1e-6 <= lon <= env_e + 1e-6
            assert env_s - 1e-6 <= lat <= env_n + 1e-6

    def test_envelope_equals_user_bbox_for_wgs84_crs(self):
        """No projection → envelope is just the original bbox."""
        bbox = _bbox(15.0, 58.0, 16.0, 58.5)
        t = CoordinateTransformer(bbox=bbox, crs="EPSG:4326")
        assert not t._use_pyproj
        env = t.wgs84_envelope_of_projected_extent()
        assert env == (bbox["west"], bbox["south"], bbox["east"], bbox["north"])

    def test_envelope_high_latitude_expansion_is_meaningful(self):
        """At Swedish latitudes the envelope must measurably expand vs. the input."""
        bbox = _bbox(15.0, 65.0, 16.0, 65.5)  # northern Sweden
        t = CoordinateTransformer(bbox=bbox, crs="EPSG:3006")

        env_w, env_s, env_e, env_n = t.wgs84_envelope_of_projected_extent()
        # Expect at least ~0.01° of expansion somewhere — at lat ~65° the grid
        # convergence vs EPSG:3006 is non-trivial across a 1° box.
        expansion = max(
            bbox["west"] - env_w,
            env_e - bbox["east"],
            bbox["south"] - env_s,
            env_n - bbox["north"],
        )
        assert expansion > 1e-4, f"envelope barely expanded ({expansion}°)"
