"""
Tests for services/global_dem/cop30_aws.py.

Covers tile-id derivation, bbox→tile-list logic, and the async fetcher's
behaviour when AWS returns 200 / 404 / non-TIFF data. Network is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from services.global_dem.cop30_aws import (  # noqa: E402
    _tile_id,
    _tile_url,
    _tiles_intersecting_bbox,
    fetch_elevation_cop30_aws,
)


class TestTileId:
    def test_northern_hemisphere_eastern(self):
        assert _tile_id(52, 13) == "Copernicus_DSM_COG_10_N52_00_E013_00_DEM"

    def test_southern_hemisphere_western(self):
        assert _tile_id(-34, -58) == "Copernicus_DSM_COG_10_S34_00_W058_00_DEM"

    def test_equator_prime_meridian(self):
        assert _tile_id(0, 0) == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"

    def test_zero_padding(self):
        assert _tile_id(5, 7) == "Copernicus_DSM_COG_10_N05_00_E007_00_DEM"
        assert _tile_id(-5, -7) == "Copernicus_DSM_COG_10_S05_00_W007_00_DEM"

    def test_url_format(self):
        url = _tile_url(52, 13)
        assert url.startswith("https://copernicus-dem-30m.s3.amazonaws.com/")
        assert url.endswith("Copernicus_DSM_COG_10_N52_00_E013_00_DEM.tif")
        # Tile ID appears twice — once as folder, once as filename
        assert url.count("Copernicus_DSM_COG_10_N52_00_E013_00_DEM") == 2


class TestTilesIntersectingBbox:
    def test_single_tile(self):
        # 0.2° wide bbox entirely inside a single 1° tile
        bbox = {"west": 13.1, "south": 52.1, "east": 13.3, "north": 52.3}
        tiles = _tiles_intersecting_bbox(bbox)
        assert tiles == [(52, 13)]

    def test_two_tiles_horizontal(self):
        # Bbox spans an east-west tile boundary (13°/14° E)
        bbox = {"west": 13.5, "south": 52.1, "east": 14.5, "north": 52.3}
        tiles = _tiles_intersecting_bbox(bbox)
        assert set(tiles) == {(52, 13), (52, 14)}

    def test_four_tiles_corner(self):
        # Bbox spans both a lat and a lon boundary
        bbox = {"west": 13.5, "south": 52.5, "east": 14.5, "north": 53.5}
        tiles = _tiles_intersecting_bbox(bbox)
        assert set(tiles) == {(52, 13), (52, 14), (53, 13), (53, 14)}

    def test_southern_hemisphere(self):
        bbox = {"west": -58.5, "south": -34.8, "east": -57.5, "north": -33.5}
        tiles = _tiles_intersecting_bbox(bbox)
        # floor(-34.8)=-35, ceil(-33.5)=-33 → lats -35,-34
        # floor(-58.5)=-59, ceil(-57.5)=-57 → lons -59,-58
        assert set(tiles) == {(-35, -59), (-35, -58), (-34, -59), (-34, -58)}

    def test_bbox_on_tile_boundary_does_not_explode(self):
        # If a bbox edge sits exactly on an integer, we shouldn't add an extra empty tile
        bbox = {"west": 13.0, "south": 52.0, "east": 13.5, "north": 52.5}
        tiles = _tiles_intersecting_bbox(bbox)
        assert tiles == [(52, 13)]


class TestFetchElevationAws:
    """Async fetch tests with httpx mocked."""

    @pytest.mark.asyncio
    async def test_returns_none_when_all_tiles_404(self):
        bbox = {"west": -10.5, "south": -10.5, "east": -10.0, "north": -10.0}
        # Mock httpx response: 404 for every tile (open ocean)
        mock_resp = AsyncMock()
        mock_resp.status_code = 404
        mock_resp.content = b""

        with patch("services.global_dem.cop30_aws.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await fetch_elevation_cop30_aws(bbox)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_response_is_not_tiff(self):
        bbox = {"west": 13.1, "south": 52.1, "east": 13.3, "north": 52.3}
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<?xml version='1.0'?><Error>Not Found</Error>"

        with patch("services.global_dem.cop30_aws.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await fetch_elevation_cop30_aws(bbox)

        assert result is None
