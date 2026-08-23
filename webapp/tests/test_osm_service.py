"""Tests for Overpass mirror selection, payload validation and query merging."""

from __future__ import annotations

import asyncio

from services.osm_service import (
    ALL_CATEGORIES,
    _accept_overpass_payload,
    _is_valid_iso_timestamp,
    _pool_for_country,
    _rank_mirrors,
    build_overpass_query,
    parse_slot_budget,
    probe_overpass_mirrors,
    split_elements_by_category,
)


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


class TestAcceptOverpassPayload:
    """A 200 OK with valid JSON is not proof of a usable response (issue #168)."""

    def _payload(self, timestamp="2026-08-23T07:44:36Z", elements=(), remark=None):
        result = {
            "osm3s": {"timestamp_osm_base": timestamp},
            "elements": list(elements),
        }
        if remark is not None:
            result["remark"] = remark
        return result

    def test_accepts_normal_response(self):
        ok, _ = _accept_overpass_payload(self._payload(elements=[{"id": 1}]), {})
        assert ok

    def test_accepts_genuinely_empty_area(self):
        # Valid timestamp, no elements, no remark: the bbox really has nothing.
        ok, _ = _accept_overpass_payload(self._payload(), {})
        assert ok

    def test_rejects_soft_error_remark(self):
        ok, reason = _accept_overpass_payload(
            self._payload(remark="runtime error: Query timed out"), {}
        )
        assert not ok and "soft error" in reason

    def test_rejects_non_iso_timestamp_with_no_elements(self):
        # overpass.osm.ch asked about Sweden: it does not hold that extract.
        ok, reason = _accept_overpass_payload(self._payload(timestamp="116600"), {})
        assert not ok and "116600" in reason

    def test_accepts_non_iso_timestamp_when_elements_are_present(self):
        # The same mirror asked about Switzerland returns real data while still
        # reporting a bare sequence number. Data beats metadata.
        ok, _ = _accept_overpass_payload(
            self._payload(timestamp="116600", elements=[{"id": 1}]),
            {"label": "osm.ch", "non_iso_timestamp": True},
        )
        assert ok


class TestParseSlotBudget:
    """Overpass advertises its per-IP concurrency budget on /api/status."""

    def test_reads_advertised_limit(self):
        assert parse_slot_budget("Connected as: 1\nRate limit: 2\n", default=4) == 2

    def test_zero_means_unlimited_but_stays_capped(self):
        # "Rate limit: 0" is no limit; we still cap it so one job can't
        # monopolise a volunteer-run server.
        assert parse_slot_budget("Rate limit: 0\n", default=4) == 4

    def test_advertised_limit_above_default_is_capped(self):
        assert parse_slot_budget("Rate limit: 16\n", default=4) == 4

    def test_missing_field_falls_back_to_default(self):
        assert parse_slot_budget("something else entirely", default=3) == 3

    def test_unparseable_body_falls_back_to_default(self):
        assert parse_slot_budget("", default=2) == 2


class TestPoolForCountry:
    """Regional mirrors hold one country's extract and must be gated on it."""

    def test_planet_pool_is_always_present(self):
        labels = [ep["label"] for ep in _pool_for_country("SE")]
        assert "overpass-api.de" in labels

    def test_regional_mirror_offered_for_its_own_country(self):
        assert "osm.ch" in [ep["label"] for ep in _pool_for_country("CH")]

    def test_regional_mirror_withheld_elsewhere(self):
        # Asking a Swiss-only mirror about Sweden returns 200 OK with zero
        # elements — a silent wrong answer, so it must never be offered.
        for country in ("SE", "NO", "PL", None):
            assert "osm.ch" not in [ep["label"] for ep in _pool_for_country(country)]

    def test_country_code_is_case_insensitive(self):
        assert "osm.ch" in [ep["label"] for ep in _pool_for_country("ch")]

    def test_kumi_is_no_longer_in_the_pool(self):
        # It is a CNAME to overpass.private.coffee; keeping both meant paying
        # two full HTTP timeouts against the same dead host (issue #168).
        urls = " ".join(ep["url"] for ep in _pool_for_country("SE"))
        assert "kumi" not in urls


def _way(osm_id, **tags):
    return {"type": "way", "id": osm_id, "tags": tags}


def _relation(osm_id, **tags):
    return {"type": "relation", "id": osm_id, "tags": tags}


class TestSplitElementsByCategory:
    """The client-side split must reproduce the five separate queries exactly.

    Merging the queries is only safe if the classifier mirrors the Overpass
    selectors it replaced — otherwise feature counts, and with them the
    surface masks, change silently. Verified against live mirrors on two real
    bboxes (33k elements, 446 relations); these cases pin the edges.
    """

    def test_each_category_claims_its_own_selectors(self):
        elements = [
            _way(1, highway="residential"),
            _way(2, natural="water"),
            _relation(3, natural="water"),
            _way(4, waterway="stream"),
            _way(5, natural="coastline"),
            _way(6, natural="wetland"),
            _way(7, natural="wood"),
            _relation(8, landuse="forest"),
            _way(9, natural="scrub"),
            _way(10, building="yes"),
            _relation(11, building="church"),
            _way(12, landuse="farmland"),
            _way(13, leisure="park"),
            _way(14, natural="grassland"),
        ]
        buckets = split_elements_by_category(elements)
        ids = {c: sorted(e["id"] for e in els) for c, els in buckets.items()}
        assert ids["roads"] == [1]
        assert ids["water"] == [2, 3, 4, 5, 6]
        assert ids["forests"] == [7, 8, 9]
        assert ids["buildings"] == [10, 11]
        assert ids["land_use"] == [12, 13, 14]

    def test_element_matching_two_categories_lands_in_both(self):
        # A shop in a retail zone matched both the buildings and the land_use
        # query before the merge, so it must still appear in both.
        buckets = split_elements_by_category([_way(1, building="retail", landuse="retail")])
        assert [e["id"] for e in buckets["buildings"]] == [1]
        assert [e["id"] for e in buckets["land_use"]] == [1]

    def test_highway_values_outside_the_whitelist_are_dropped(self):
        # The selector is an anchored regex, not a bare key test: proposed and
        # construction roads must not become terrain.
        elements = [_way(1, highway="proposed"), _way(2, highway="construction"),
                    _way(3, highway="raceway"), _way(4, highway="motorway")]
        assert [e["id"] for e in split_elements_by_category(elements)["roads"]] == [4]

    def test_landuse_values_outside_the_whitelist_are_dropped(self):
        elements = [_way(1, landuse="grass"), _way(2, landuse="meadow")]
        assert [e["id"] for e in split_elements_by_category(elements)["land_use"]] == [2]

    def test_nodes_are_never_claimed(self):
        # Every selector is way/relation only; `out body geom` still returns
        # nodes for some queries and they must not be processed as features.
        node = {"type": "node", "id": 1, "tags": {"building": "yes", "highway": "residential"}}
        buckets = split_elements_by_category([node])
        assert all(not els for els in buckets.values())

    def test_relation_only_selectors_are_respected(self):
        # `way["natural"="scrub"]` has no relation counterpart, and
        # `way["leisure"=...]` has none either.
        elements = [_relation(1, natural="scrub"), _relation(2, leisure="park"),
                    _relation(3, natural="grassland")]
        buckets = split_elements_by_category(elements)
        assert not buckets["forests"]
        assert not buckets["land_use"]

    def test_untagged_element_matches_nothing(self):
        buckets = split_elements_by_category([{"type": "way", "id": 1}])
        assert all(not els for els in buckets.values())

    def test_restricting_categories_returns_only_those(self):
        buckets = split_elements_by_category(
            [_way(1, highway="residential"), _way(2, building="yes")], ["roads"]
        )
        assert list(buckets) == ["roads"]


class TestBuildOverpassQuery:
    """One query builder feeds both the merged path and the per-category path."""

    def test_merged_query_covers_every_category(self):
        query = build_overpass_query(
            {"south": 1.0, "west": 2.0, "north": 3.0, "east": 4.0}, ALL_CATEGORIES
        )
        for fragment in ('"highway"', '"building"', '"waterway"', '"landuse"="forest"', '"leisure"'):
            assert fragment in query
        # A single union and a single output statement — that is the whole
        # point: one query slot instead of five (issue #168).
        assert query.count("out body geom;") == 1

    def test_single_category_query_excludes_the_others(self):
        query = build_overpass_query({"south": 1.0, "west": 2.0, "north": 3.0, "east": 4.0}, ["roads"])
        assert '"highway"' in query
        assert '"building"' not in query

    def test_bbox_is_emitted_south_west_north_east(self):
        query = build_overpass_query({"south": 1.5, "west": 2.5, "north": 3.5, "east": 4.5}, ["roads"])
        assert "[bbox:1.5,2.5,3.5,4.5]" in query

    def test_timeout_override_is_applied(self):
        query = build_overpass_query(
            {"south": 1.0, "west": 2.0, "north": 3.0, "east": 4.0}, ["roads"], timeout=180
        )
        assert "[timeout:180]" in query


class TestProbeDropsUnhealthyMirrors:
    """Issue #168: the probe's verdict has to actually be enforced."""

    def _probe(self, monkeypatch, verdicts):
        """Run probe_overpass_mirrors with mirror health stubbed by label."""
        async def fake_probe(client, endpoint):
            healthy, latency = verdicts[endpoint["label"]]
            return endpoint, healthy, latency

        monkeypatch.setattr("services.osm_service._probe_one_mirror", fake_probe)
        return asyncio.run(probe_overpass_mirrors(country="SE"))

    def test_unhealthy_mirrors_are_dropped_not_demoted(self, monkeypatch):
        # The reported failure: the probe found 1 of 4 healthy, then the query
        # loop walked the dead ones anyway and burned 2m35s.
        ordered = self._probe(monkeypatch, {
            "overpass-api.de": (True, 0.8),
            "Private.coffee": (False, 12.6),
            "VK Maps": (False, 12.6),
        })
        assert [ep["label"] for ep in ordered] == ["overpass-api.de"]

    def test_healthy_mirrors_are_ordered_fastest_first(self, monkeypatch):
        ordered = self._probe(monkeypatch, {
            "overpass-api.de": (True, 2.0),
            "Private.coffee": (True, 0.3),
            "VK Maps": (True, 1.0),
        })
        assert [ep["label"] for ep in ordered] == ["Private.coffee", "VK Maps", "overpass-api.de"]

    def test_total_probe_failure_falls_back_to_the_whole_pool(self, monkeypatch):
        # A probe-wide failure says more about our network than the mirrors,
        # so nothing is dropped in that case.
        ordered = self._probe(monkeypatch, {
            "overpass-api.de": (False, 12.0),
            "Private.coffee": (False, 12.0),
            "VK Maps": (False, 12.0),
        })
        assert len(ordered) == 3
