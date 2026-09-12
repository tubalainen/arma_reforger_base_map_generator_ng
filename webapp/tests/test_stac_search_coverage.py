"""
STAC Bild tile selection must be driven by coverage, not by a fixed count.

The search is sorted newest-first by acquisition *time*, not by position, so
"the newest N items" is not "the items covering the map". On the Hammarö map
the catalogue held 151 items; the code took the first 50 and left the entire
south-east corner unrequested. The mosaic shipped 6.8% solid black with **zero
download failures** — confirmed in the container log:

    STAC Bild: search returned 50 item(s)
    phase 1 done in 30.6s — 50 ok, 0 deferred
    merged orthophoto is 6.8% nodata (0 tile(s) permanently failed)

Coverage is a property of which ground the tiles occupy. A bigger fixed limit
does not buy it — it just downloads more tiles and hopes. Walking the pages and
keeping only items that contribute new ground gets 100% coverage from *fewer*
downloads than the old single page: 12 instead of 50 on that map, because 11
tiles from 2024 plus one from 2012 cover everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


def _tile(x0, y0, x1, y1, when="2024-05-04T11:00:00Z"):
    return {"bbox": [x0, y0, x1, y1], "properties": {"datetime": when}}


class TestSelectCoveringItems:
    def test_redundant_tiles_are_dropped(self):
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        feats = [
            _tile(0, 0, 1, 1, "2024-01-01T00:00:00Z"),
            _tile(1, 0, 2, 1, "2022-01-01T00:00:00Z"),
            _tile(0, 0, 2, 1, "2010-01-01T00:00:00Z"),  # adds nothing new
        ]
        kept, covered = select_covering_items(feats, (0, 0, 2, 1))
        assert len(kept) == 2
        assert covered == pytest.approx(1.0)

    def test_an_older_tile_is_kept_when_it_fills_a_gap(self):
        """The exact Hammarö shape: the newest epoch does not reach one
        corner, so an older tile must survive the selection."""
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        feats = [
            _tile(0, 0.5, 2, 1, "2024-01-01T00:00:00Z"),  # north half only
            _tile(0, 0, 2, 0.5, "2012-01-01T00:00:00Z"),  # the missing south
        ]
        kept, covered = select_covering_items(feats, (0, 0, 2, 1))
        assert len(kept) == 2, "the older tile fills the gap and must be kept"
        assert covered == pytest.approx(1.0)

    def test_partial_catalogue_coverage_is_reported_not_hidden(self):
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        kept, covered = select_covering_items(
            [_tile(0, 0, 1, 1)], (0, 0, 2, 1)
        )
        assert len(kept) == 1
        assert covered == pytest.approx(0.5, abs=0.01)

    def test_newest_wins_where_both_cover(self):
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        newest = _tile(0, 0, 2, 1, "2024-01-01T00:00:00Z")
        older = _tile(0, 0, 2, 1, "2010-01-01T00:00:00Z")
        kept, _ = select_covering_items([newest, older], (0, 0, 2, 1))
        assert kept == [newest]

    def test_an_item_without_a_footprint_is_never_discarded(self):
        """Better to download a tile we can't reason about than to drop one
        that might have been the only cover for a corner. (Once the area is
        fully covered the walk stops early, so put it before that point.)"""
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        feats = [
            _tile(0, 0, 1, 1),                      # half the area
            {"properties": {"datetime": "2020"}},   # no bbox to reason about
            _tile(1, 0, 2, 1),                      # the other half
        ]
        kept, covered = select_covering_items(feats, (0, 0, 2, 1))
        assert len(kept) == 3, "the footprint-less item must be kept"
        assert covered == pytest.approx(1.0)

    def test_a_degenerate_bbox_does_not_crash(self):
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        kept, covered = select_covering_items([_tile(0, 0, 1, 1)], (1, 1, 1, 1))
        assert covered == 0.0
        assert len(kept) == 1


class TestSearchPaging:
    def test_a_next_link_body_is_extracted(self):
        from services.lantmateriet.stac_orthophoto_service import (
            _next_search_body,
        )

        body = {"bbox": [1, 2, 3, 4], "limit": 50, "token": "page2"}
        payload = {
            "features": [],
            "links": [
                {"rel": "self", "href": "x"},
                {"rel": "next", "href": "y", "body": body},
            ],
        }
        assert _next_search_body(payload) == body

    def test_the_last_page_has_no_next_body(self):
        from services.lantmateriet.stac_orthophoto_service import (
            _next_search_body,
        )

        assert _next_search_body({"features": [], "links": []}) is None
        assert _next_search_body({"features": []}) is None
        # A next link with no body is not usable for a POST search.
        assert (
            _next_search_body({"links": [{"rel": "next", "href": "y"}]}) is None
        )

    def test_paging_is_bounded(self):
        """The catalogue holds every epoch ever flown — an unbounded walk
        would pull hundreds of items for a large map."""
        from services.lantmateriet.stac_orthophoto_service import (
            STAC_SEARCH_MAX_PAGES,
            STAC_SEARCH_PAGE_SIZE,
        )

        assert 1 < STAC_SEARCH_MAX_PAGES <= 20
        assert STAC_SEARCH_PAGE_SIZE >= 50


class TestTheHammaroRegression:
    """Reconstructs the failure from the real catalogue's shape: the items
    covering one corner sit past the first page, because the sort is by time.
    """

    def _catalogue(self):
        """Page 1 mirrors the real catalogue's shape: several epochs that all
        re-cover the same northern ground (2024, 2022, 2020, 2018 each flew
        it), none reaching the south-east corner. The corner exists only in a
        much older epoch, which sorts past page 1.
        """
        feats = []
        for year in ("2024", "2022", "2020", "2018"):
            for i in range(12):            # 12 tiles blanket the north half
                x = (i % 4) * 0.25
                y = 0.5 + (i // 4) * 0.15
                feats.append(
                    _tile(x, y, x + 0.30, y + 0.20, f"{year}-01-01T00:00:00Z")
                )
        corner = _tile(0.0, 0.0, 1.0, 0.55, "2012-01-01T00:00:00Z")
        return feats, corner

    def test_one_page_leaves_the_corner_uncovered(self):
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        page1, _ = self._catalogue()
        _, covered = select_covering_items(page1, (0, 0, 1, 1))
        assert covered < 0.95, (
            "fixture must actually reproduce the gap, or this proves nothing"
        )

    def test_paging_to_the_corner_tile_completes_coverage(self):
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        page1, corner = self._catalogue()
        _, covered_before = select_covering_items(page1, (0, 0, 1, 1))
        kept, covered_after = select_covering_items(page1 + [corner], (0, 0, 1, 1))

        assert covered_after > covered_before
        assert corner in kept, "the corner tile must survive selection"

    def test_selection_downloads_fewer_tiles_than_the_whole_page(self):
        """The fix must not cost more downloads than it saves. On the real
        map it was 12 tiles instead of 50, because the older epochs re-cover
        ground the newest one already has."""
        from services.lantmateriet.stac_orthophoto_service import (
            select_covering_items,
        )

        page1, corner = self._catalogue()
        kept, covered = select_covering_items(page1 + [corner], (0, 0, 1, 1))
        assert covered == pytest.approx(1.0)
        assert len(kept) < len(page1), (
            f"kept {len(kept)} of {len(page1)} page-1 items — the redundant "
            f"epochs should have been pruned"
        )
