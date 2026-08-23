"""Tests for the on-disk Overpass response cache.

Overpass is the slowest and least reliable step in the pipeline, so a repeat
generation of the same square should never touch the network. Every failure
mode here must degrade to a miss rather than break a generation.
"""

from __future__ import annotations

import gzip
import json
import os
import time

import pytest

from services import overpass_cache


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache at a throwaway directory for each test."""
    directory = tmp_path / "overpass_cache"
    monkeypatch.setattr(overpass_cache, "CACHE_DIR", directory)
    monkeypatch.setattr(overpass_cache, "OVERPASS_CACHE_ENABLED", True)
    monkeypatch.setattr(overpass_cache, "OVERPASS_CACHE_TTL_HOURS", 24)
    monkeypatch.setattr(overpass_cache, "OVERPASS_CACHE_MAX_MB", 512)
    return directory


PAYLOAD = {
    "version": 0.6,
    "osm3s": {"timestamp_osm_base": "2026-08-23T07:44:36Z"},
    "elements": [{"type": "way", "id": 1, "tags": {"highway": "residential"}}],
}


class TestRoundTrip:
    def test_store_then_load_returns_the_payload(self, cache_dir):
        overpass_cache.store("[out:json];way(1);out;", PAYLOAD)
        assert overpass_cache.load("[out:json];way(1);out;") == PAYLOAD

    def test_miss_returns_none(self, cache_dir):
        assert overpass_cache.load("[out:json];way(999);out;") is None

    def test_different_queries_do_not_collide(self, cache_dir):
        overpass_cache.store("query A", {"elements": [{"id": 1}]})
        overpass_cache.store("query B", {"elements": [{"id": 2}]})
        assert overpass_cache.load("query A")["elements"] == [{"id": 1}]
        assert overpass_cache.load("query B")["elements"] == [{"id": 2}]

    def test_whitespace_differences_hit_the_same_entry(self, cache_dir):
        # Queries are built from an indented template; reindenting it must not
        # invalidate every cached entry.
        overpass_cache.store("[out:json];\n  way(1);\nout;", PAYLOAD)
        assert overpass_cache.load("[out:json]; way(1); out;") == PAYLOAD

    def test_bbox_is_part_of_the_key(self, cache_dir):
        # The bbox lives inside the query string, so two areas never share one.
        overpass_cache.store("[bbox:1,2,3,4];way;out;", {"elements": [{"id": 1}]})
        assert overpass_cache.load("[bbox:5,6,7,8];way;out;") is None


class TestExpiry:
    def test_entry_older_than_the_ttl_is_a_miss(self, cache_dir, monkeypatch):
        overpass_cache.store("query", PAYLOAD)
        path = overpass_cache._entry_path(overpass_cache._cache_key("query"))
        stale = time.time() - 25 * 3600
        os.utime(path, (stale, stale))
        assert overpass_cache.load("query") is None

    def test_expired_entry_is_deleted(self, cache_dir):
        overpass_cache.store("query", PAYLOAD)
        path = overpass_cache._entry_path(overpass_cache._cache_key("query"))
        stale = time.time() - 25 * 3600
        os.utime(path, (stale, stale))
        overpass_cache.load("query")
        assert not path.exists()

    def test_entry_inside_the_ttl_still_hits(self, cache_dir):
        overpass_cache.store("query", PAYLOAD)
        path = overpass_cache._entry_path(overpass_cache._cache_key("query"))
        recent = time.time() - 3600
        os.utime(path, (recent, recent))
        assert overpass_cache.load("query") == PAYLOAD


class TestFailuresAreNonFatal:
    def test_corrupt_entry_is_a_miss_not_an_exception(self, cache_dir):
        overpass_cache.store("query", PAYLOAD)
        path = overpass_cache._entry_path(overpass_cache._cache_key("query"))
        path.write_bytes(b"this is not gzip")
        assert overpass_cache.load("query") is None

    def test_corrupt_entry_is_removed(self, cache_dir):
        overpass_cache.store("query", PAYLOAD)
        path = overpass_cache._entry_path(overpass_cache._cache_key("query"))
        path.write_bytes(b"this is not gzip")
        overpass_cache.load("query")
        assert not path.exists()

    def test_truncated_gzip_is_a_miss(self, cache_dir):
        overpass_cache.store("query", PAYLOAD)
        path = overpass_cache._entry_path(overpass_cache._cache_key("query"))
        path.write_bytes(gzip.compress(b'{"elements": [')[:10])
        assert overpass_cache.load("query") is None

    def test_unwritable_cache_dir_does_not_raise(self, cache_dir, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(overpass_cache.Path, "mkdir", boom)
        overpass_cache.store("query", PAYLOAD)  # must not raise
        assert overpass_cache.load("query") is None

    def test_no_partial_entry_survives_a_failed_write(self, cache_dir):
        class Unserialisable:
            pass

        overpass_cache.store("query", {"elements": [Unserialisable()]})
        assert overpass_cache.load("query") is None
        assert not list(cache_dir.glob("*.tmp"))


class TestDisabled:
    def test_disabled_cache_never_stores(self, cache_dir, monkeypatch):
        monkeypatch.setattr(overpass_cache, "OVERPASS_CACHE_ENABLED", False)
        overpass_cache.store("query", PAYLOAD)
        assert not cache_dir.exists() or not list(cache_dir.glob("*.json.gz"))

    def test_disabled_cache_never_loads(self, cache_dir, monkeypatch):
        overpass_cache.store("query", PAYLOAD)
        monkeypatch.setattr(overpass_cache, "OVERPASS_CACHE_ENABLED", False)
        assert overpass_cache.load("query") is None


class TestEviction:
    def test_oldest_entries_are_evicted_over_the_cap(self, cache_dir, monkeypatch):
        # Cap of 0 MB forces eviction of everything but the entry just written.
        big = {"elements": [{"id": i, "tags": {"name": "x" * 200}} for i in range(500)]}
        overpass_cache.store("old", big)
        old_path = overpass_cache._entry_path(overpass_cache._cache_key("old"))
        stale = time.time() - 100
        os.utime(old_path, (stale, stale))

        monkeypatch.setattr(overpass_cache, "OVERPASS_CACHE_MAX_MB", 0)
        overpass_cache.store("new", big)

        assert not old_path.exists()
