"""Tests for Overpass mirror health validation and probe-based ranking."""

from __future__ import annotations

from services.osm_service import _is_valid_iso_timestamp, _rank_mirrors


class TestIsValidIsoTimestamp:
    """The signal that distinguishes a healthy mirror from a broken one (issue #131)."""

    def test_accepts_real_overpass_timestamp(self):
        assert _is_valid_iso_timestamp("2026-05-20T15:27:44Z")

    def test_accepts_timestamp_without_trailing_z(self):
        assert _is_valid_iso_timestamp("2026-05-20T15:27:44")

    def test_rejects_bare_integer_counter(self):
        # The exact corruption observed from overpass.osm.ch.
        assert not _is_valid_iso_timestamp("114329")

    def test_rejects_empty_string(self):
        assert not _is_valid_iso_timestamp("")

    def test_rejects_non_string(self):
        assert not _is_valid_iso_timestamp(None)


class TestRankMirrors:
    """Probe results must order healthy mirrors fastest-first, broken ones last."""

    def test_healthy_sorted_by_ascending_latency(self):
        results = [
            ("https://a/api", True, 2.0),
            ("https://b/api", True, 0.5),
            ("https://c/api", True, 1.0),
        ]
        assert _rank_mirrors(results) == [
            "https://b/api",
            "https://c/api",
            "https://a/api",
        ]

    def test_unhealthy_demoted_to_back_in_pool_order(self):
        results = [
            ("https://a/api", False, 12.0),
            ("https://b/api", True, 1.5),
            ("https://c/api", False, 0.1),
            ("https://d/api", True, 0.3),
        ]
        # Healthy mirrors (by latency) first, then unhealthy ones in pool order.
        assert _rank_mirrors(results) == [
            "https://d/api",
            "https://b/api",
            "https://a/api",
            "https://c/api",
        ]

    def test_all_unhealthy_preserves_pool_order(self):
        results = [
            ("https://a/api", False, 12.0),
            ("https://b/api", False, 12.0),
        ]
        assert _rank_mirrors(results) == ["https://a/api", "https://b/api"]

    def test_single_healthy_mirror(self):
        results = [("https://only/api", True, 0.4)]
        assert _rank_mirrors(results) == ["https://only/api"]

    def test_empty_results(self):
        assert _rank_mirrors([]) == []
