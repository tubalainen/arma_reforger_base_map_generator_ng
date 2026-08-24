"""Tests for the optional self-hosted Overpass sidecar.

The sidecar is opt-in, holds one country extract rather than the planet, and
takes hours to import. Each of those is a way for it to be silently wrong, so
the tests below pin the three behaviours that matter: it is never used outside
its coverage, a configuration change is never silently ignored, and its
absence never breaks a generation.
"""

from __future__ import annotations

import asyncio

import pytest

from config import overpass_local as cfg
from services import overpass_local as svc
from services.osm_service import _pool_with_local


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test with the sidecar switched off."""
    for var in (
        "OVERPASS_LOCAL_COUNTRIES",
        "OVERPASS_LOCAL_REGION",
        "OVERPASS_LOCAL_URL",
        "OVERPASS_LOCAL_ONLY",
        "OVERPASS_LOCAL_MARKER_PATH",
        "OVERPASS_LOCAL_MIRROR",
        "OVERPASS_PLANET_URL",
        "OVERPASS_DIFF_URL",
        "OVERPASS_UPDATE_SLEEP",
        "OVERPASS_LOCAL_STALE_AFTER_HOURS",
    ):
        monkeypatch.delenv(var, raising=False)


class TestRegionResolution:
    def test_disabled_by_default(self):
        assert cfg.local_region() == ""
        assert not cfg.local_enabled()

    def test_single_country_resolves_to_its_extract(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        assert cfg.local_region() == "europe/sweden"
        assert cfg.local_enabled()

    def test_country_code_is_case_and_space_insensitive(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", " se ")
        assert cfg.local_region() == "europe/sweden"

    def test_explicit_region_overrides_country(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setenv("OVERPASS_LOCAL_REGION", "europe")
        assert cfg.local_region() == "europe"

    def test_multiple_countries_is_an_error(self, monkeypatch):
        # One sidecar holds one extract. Silently resolving "SE,NO" to the
        # europe extract would mean a surprise 32 GB download.
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE,NO")
        with pytest.raises(cfg.LocalOverpassConfigError) as exc:
            cfg.local_region()
        assert "OVERPASS_LOCAL_REGION=europe" in str(exc.value)

    def test_unknown_country_is_an_error(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "XX")
        with pytest.raises(cfg.LocalOverpassConfigError):
            cfg.local_region()

    def test_broken_config_still_counts_as_enabled(self, monkeypatch):
        # The operator meant to turn it on; the error belongs in the status
        # report, not in silently falling back as though it were never set.
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE,NO")
        assert cfg.local_enabled()

    def test_every_mapped_extract_has_a_size(self):
        for country, (path, size) in cfg.GEOFABRIK_EXTRACTS.items():
            assert path and size > 0, country

    def test_extract_size_is_reported(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "IS")
        assert cfg.local_extract_size_gb() == pytest.approx(0.06)


class TestStatus:
    def _status(self, monkeypatch, responder, marker=None, tmp_path=None):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        if marker is not None:
            path = tmp_path / "region.txt"
            path.write_text(marker, encoding="utf-8")
            monkeypatch.setenv("OVERPASS_LOCAL_MARKER_PATH", str(path))
        else:
            monkeypatch.setenv("OVERPASS_LOCAL_MARKER_PATH", "/nonexistent/region.txt")

        class FakeResponse:
            status_code, _payload = responder
            def json(self):
                return self._payload

        class FakeClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, *a, **k):
                if isinstance(responder, Exception):
                    raise responder
                return FakeResponse()

        monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: FakeClient())
        return asyncio.run(svc.get_local_status())

    def test_disabled_when_unconfigured(self):
        status = asyncio.run(svc.get_local_status())
        assert status["state"] == "disabled"
        assert status["enabled"] is False

    def test_misconfigured_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE,NO")
        status = asyncio.run(svc.get_local_status())
        assert status["state"] == "misconfigured"
        assert "one extract" in status["message"]

    def test_ready_when_serving_fresh_data(self, monkeypatch, tmp_path):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = self._status(
            monkeypatch,
            (200, {"osm3s": {"timestamp_osm_base": now}, "elements": []}),
            marker="europe/sweden", tmp_path=tmp_path,
        )
        assert status["state"] == "ready"

    def test_stale_when_the_diff_loop_stalls(self, monkeypatch, tmp_path):
        status = self._status(
            monkeypatch,
            (200, {"osm3s": {"timestamp_osm_base": "2026-01-01T00:00:00Z"}, "elements": []}),
            marker="europe/sweden", tmp_path=tmp_path,
        )
        assert status["state"] == "stale"
        assert "update loop may be stuck" in status["message"]

    def test_restart_required_when_config_changed(self, monkeypatch, tmp_path):
        # .env now says SE, but the running container built Norway.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = self._status(
            monkeypatch,
            (200, {"osm3s": {"timestamp_osm_base": now}, "elements": []}),
            marker="europe/norway", tmp_path=tmp_path,
        )
        assert status["state"] == "restart_required"
        assert "europe/norway" in status["message"]
        assert "europe/sweden" in status["message"]

    def test_importing_when_not_answering_yet(self, monkeypatch, tmp_path):
        status = self._status(
            monkeypatch, (503, {}), marker=None, tmp_path=tmp_path
        )
        assert status["state"] == "importing"

    def test_missing_marker_does_not_claim_a_mismatch(self, monkeypatch, tmp_path):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = self._status(
            monkeypatch,
            (200, {"osm3s": {"timestamp_osm_base": now}, "elements": []}),
            marker=None, tmp_path=tmp_path,
        )
        assert status["state"] == "ready"


class TestPoolIntegration:
    """The sidecar must never be used outside the countries it covers."""

    def _pool(self, monkeypatch, country, ready=True):
        async def fake_endpoint():
            return {"url": "http://local/api/interpreter", "label": "Local Overpass",
                    "slots": 8, "local": True} if ready else None
        monkeypatch.setattr("services.osm_service.local_endpoint_if_ready", fake_endpoint)
        return asyncio.run(_pool_with_local(country))

    def test_absent_sidecar_leaves_the_public_pool_untouched(self, monkeypatch):
        labels = [ep["label"] for ep in asyncio.run(_pool_with_local("SE"))]
        assert "Local Overpass" not in labels
        assert "overpass-api.de" in labels

    def test_used_for_a_covered_country(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        labels = [ep["label"] for ep in self._pool(monkeypatch, "SE")]
        assert labels[0] == "Local Overpass"
        assert "overpass-api.de" in labels  # public mirrors remain as fallback

    def test_skipped_outside_its_coverage(self, monkeypatch):
        # A Sweden extract answers a French bbox with zero elements, which is
        # indistinguishable from "no roads here" — so it must not be offered.
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        labels = [ep["label"] for ep in self._pool(monkeypatch, "FR")]
        assert "Local Overpass" not in labels
        assert "overpass-api.de" in labels

    def test_skipped_while_still_importing(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        labels = [ep["label"] for ep in self._pool(monkeypatch, "SE", ready=False)]
        assert "Local Overpass" not in labels
        assert "overpass-api.de" in labels

    def test_local_only_drops_the_public_mirrors(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setenv("OVERPASS_LOCAL_ONLY", "1")
        labels = [ep["label"] for ep in self._pool(monkeypatch, "SE")]
        assert labels == ["Local Overpass"]

    def test_local_only_yields_nothing_when_the_sidecar_is_down(self, monkeypatch):
        # Better an explicit "no mirrors" failure than silently reaching out
        # to third parties a private deployment has forbidden.
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setenv("OVERPASS_LOCAL_ONLY", "1")
        assert self._pool(monkeypatch, "SE", ready=False) == []

    def test_broken_config_falls_back_to_public_mirrors(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE,NO")
        labels = [ep["label"] for ep in self._pool(monkeypatch, "SE")]
        assert "Local Overpass" not in labels
        assert "overpass-api.de" in labels


class TestLauncherGeneration:
    """The sidecar's launcher is written by the init step, not docker-compose.

    That placement is deliberate: operators who deploy from GHCR with
    `docker compose pull` and hand-maintain their compose file get the PBF
    conversion from the image, with nothing to merge by hand.
    """

    def _launcher(self, tmp_path, monkeypatch, country="SE"):
        import scripts.overpass_local_init as init
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", country)
        monkeypatch.setattr(init, "DB_DIR", tmp_path / "db")
        monkeypatch.setattr(init, "META_DIR", tmp_path / "meta")
        (tmp_path / "db").mkdir(exist_ok=True)
        assert init.main() == 0
        return (tmp_path / "meta" / "start.sh").read_text(encoding="utf-8")

    def test_launcher_sets_the_resolved_urls(self, tmp_path, monkeypatch):
        script = self._launcher(tmp_path, monkeypatch)
        assert "europe/sweden-latest.osm.pbf" in script
        assert "europe/sweden-updates/" in script

    def test_launcher_carries_the_pbf_conversion(self, tmp_path, monkeypatch):
        # Without this the importer's `bunzip2 < planet.osm.bz2` is handed a
        # PBF and the whole import dies — the v1.10.0 failure. The conversion
        # moved into a script when it grew a guard and a cache, so the launcher
        # only has to point at it.
        script = self._launcher(tmp_path, monkeypatch)
        assert "/overpass_meta/preprocess.sh" in script
        preprocess = (tmp_path / "meta" / "preprocess.sh").read_text(encoding="utf-8")
        assert "osmium cat" in preprocess
        assert "/db/planet.osm.bz2" in preprocess

    def test_conversion_default_does_not_clobber_an_override(self, tmp_path, monkeypatch):
        # ":=" assigns only when unset or empty, so a value from docker-compose
        # still wins.
        script = self._launcher(tmp_path, monkeypatch)
        assert ': "${OVERPASS_PLANET_PREPROCESS:=' in script
        assert "export OVERPASS_PLANET_PREPROCESS" in script

    def test_launcher_execs_the_images_own_entrypoint(self, tmp_path, monkeypatch):
        script = self._launcher(tmp_path, monkeypatch)
        assert script.rstrip().endswith("exec /app/docker-entrypoint.sh")

    def test_launcher_is_valid_posix_shell(self, tmp_path, monkeypatch):
        import shutil
        import subprocess
        sh = shutil.which("sh")
        if not sh:
            import pytest as _pytest
            _pytest.skip("no POSIX shell available")
        self._launcher(tmp_path, monkeypatch)
        result = subprocess.run(
            [sh, "-n", str(tmp_path / "meta" / "start.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestMirrorSelection:
    """Geofabrik firewalls IPs that re-download extracts, so there has to be a
    way off it that does not involve hand-editing docker-compose.yml."""

    def test_geofabrik_is_the_default(self):
        assert cfg.mirror() == "geofabrik"

    def test_unknown_mirror_is_an_error(self, monkeypatch):
        # Silently falling back would send a blocked operator straight back to
        # the mirror that blocked them.
        monkeypatch.setenv("OVERPASS_LOCAL_MIRROR", "geofabrick")
        with pytest.raises(cfg.LocalOverpassConfigError):
            cfg.mirror()

    def test_urls_follow_the_selected_mirror(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        assert cfg.planet_url().startswith(cfg.GEOFABRIK_BASE)
        monkeypatch.setenv("OVERPASS_LOCAL_MIRROR", "osmfr")
        assert cfg.planet_url() == (
            "https://download.openstreetmap.fr/extracts/europe/sweden-latest.osm.pbf"
        )
        assert cfg.diff_url() == (
            "https://download.openstreetmap.fr/replication/europe/sweden/minute/"
        )

    def test_region_identity_is_mirror_independent(self, monkeypatch):
        # The marker records the canonical region, so switching mirrors must
        # not read as a region change and trigger a multi-hour re-import.
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setenv("OVERPASS_LOCAL_MIRROR", "osmfr")
        assert cfg.local_region() == "europe/sweden"
        assert cfg.mirror_region() == "europe/sweden"

    def test_osmfr_translates_slugs_that_differ(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_MIRROR", "osmfr")
        for country, expected in (
            ("GB", "europe/united_kingdom"),
            ("CZ", "europe/czech_republic"),
            ("IE", "europe/ireland"),
            ("RU", "russia"),
        ):
            monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", country)
            assert cfg.mirror_region() == expected

    def test_osmfr_gap_is_an_error_not_a_404_later(self, monkeypatch):
        # OSM France publishes no Baltic or Balkan country extracts. Better a
        # config error now than a 404 six hours into a container restart loop.
        monkeypatch.setenv("OVERPASS_LOCAL_MIRROR", "osmfr")
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "EE")
        with pytest.raises(cfg.LocalOverpassConfigError):
            cfg.mirror_region()

    def test_every_osmfr_entry_maps_a_real_geofabrik_region(self):
        known = {path for path, _ in cfg.GEOFABRIK_EXTRACTS.values()}
        known |= set(cfg.PARENT_EXTRACTS)
        assert set(cfg.OSMFR_REGIONS) <= known

    def test_explicit_urls_override_the_mirror(self, monkeypatch):
        # The full escape hatch: a private mirror, or a hand-downloaded extract
        # dropped on the volume by an operator Geofabrik has blocked.
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setenv("OVERPASS_PLANET_URL", "file:///db/mine.osm.pbf")
        monkeypatch.setenv("OVERPASS_DIFF_URL", "https://example.org/replication/")
        assert cfg.planet_url() == "file:///db/mine.osm.pbf"
        assert cfg.diff_url() == "https://example.org/replication/"

    def test_explicit_region_is_passed_through_untranslated(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_MIRROR", "osmfr")
        monkeypatch.setenv("OVERPASS_LOCAL_REGION", "asia/japan")
        assert cfg.mirror_region() == "asia/japan"


class TestUpdateCadence:
    """The mirrors are volunteer-run; one deployment must not become a stream
    of requests against them."""

    def test_defaults_to_weekly(self):
        assert cfg.update_sleep_seconds() == 7 * 24 * 3600

    def test_operator_value_is_honoured(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_UPDATE_SLEEP", "86400")
        assert cfg.update_sleep_seconds() == 86400

    def test_too_eager_a_value_is_clamped(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_UPDATE_SLEEP", "60")
        assert cfg.update_sleep_seconds() == cfg.MIN_UPDATE_SLEEP_SECONDS

    def test_garbage_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_UPDATE_SLEEP", "soon")
        assert cfg.update_sleep_seconds() == cfg.DEFAULT_UPDATE_SLEEP_SECONDS

    def test_stale_threshold_outlives_one_sweep(self, monkeypatch):
        # Otherwise a healthy weekly sidecar reports "stale" most of the week.
        assert cfg.local_stale_after_hours() > cfg.update_sleep_seconds() / 3600
        monkeypatch.setenv("OVERPASS_UPDATE_SLEEP", "86400")
        assert cfg.local_stale_after_hours() == 24 + cfg.STALE_GRACE_HOURS

    def test_explicit_stale_threshold_still_wins(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_LOCAL_STALE_AFTER_HOURS", "12")
        assert cfg.local_stale_after_hours() == 12


class TestDownloadGuardAndCache:
    """The upstream entrypoint reports curl exit 000 as success — that is the
    `file://` scheme's code, and also what a firewalled mirror produces. Every
    failed import then re-downloads the whole extract on the next start, which
    is how an IP gets blocked in the first place."""

    def _run(self, tmp_path, contents, cache=True):
        """Run preprocess.sh against a fake download, return (rc, output)."""
        import shutil
        import subprocess
        sh = shutil.which("sh")
        if not sh:
            pytest.skip("no POSIX shell available")

        db = tmp_path / "db"
        db.mkdir(exist_ok=True)
        (db / "planet.osm.bz2").write_bytes(contents)

        script = tmp_path / "preprocess.sh"
        script.write_text(
            cfg.PREPROCESS_SCRIPT.replace("/db/", f"{db.as_posix()}/"),
            encoding="utf-8",
            newline="\n",
        )

        # osmium is not on the host. Stub it rather than editing the script, so
        # what runs here is the script as shipped.
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "osmium"
        stub.write_text(
            "#!/bin/sh\n"
            "# real call: osmium cat --overwrite -o DST SRC\n"
            "shift 3\n"
            'cp "$2" "$1"\n',
            encoding="utf-8",
            newline="\n",
        )
        stub.chmod(0o755)

        env = {
            "PATH": f"{stub_dir.as_posix()}:/usr/bin:/bin",
            "OVERPASS_PLANET_URL": "https://example.org/sweden-latest.osm.pbf",
        }
        if cache:
            env["OVERPASS_EXTRACT_CACHE"] = (
                tmp_path / "db" / "extract_cache" / "planet-europe_sweden.osm.pbf"
            ).as_posix()
        result = subprocess.run(
            [sh, str(script)], capture_output=True, text=True, env=env, errors="replace"
        )
        return result.returncode, result.stdout + result.stderr

    def test_is_valid_posix_shell(self, tmp_path):
        import shutil
        import subprocess
        sh = shutil.which("sh")
        if not sh:
            pytest.skip("no POSIX shell available")
        script = tmp_path / "preprocess.sh"
        script.write_text(cfg.PREPROCESS_SCRIPT, encoding="utf-8", newline="\n")
        result = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_empty_download_fails_loudly(self, tmp_path):
        rc, out = self._run(tmp_path, b"")
        assert rc != 0
        assert "empty" in out
        # The message has to point at the network, not at the file — that is
        # the whole reason the guard exists.
        assert "example.org" in out

    def test_error_page_is_not_mistaken_for_an_extract(self, tmp_path):
        rc, out = self._run(tmp_path, b"<html><title>403 Forbidden</title></html>")
        assert rc != 0
        assert "not an OSM PBF" in out

    def test_good_download_is_cached(self, tmp_path):
        rc, out = self._run(tmp_path, b"\x00\x00\x00\x0d\n\tOSMHeader\x18" + b"x" * 512)
        assert rc == 0, out
        cached = tmp_path / "db" / "extract_cache" / "planet-europe_sweden.osm.pbf"
        assert cached.is_file()
        assert cached.stat().st_size > 512

    def test_no_cache_configured_still_converts(self, tmp_path):
        rc, out = self._run(
            tmp_path, b"\x00\x00\x00\x0d\n\tOSMHeader\x18" + b"x" * 512, cache=False
        )
        assert rc == 0, out


class TestExtractCache:
    def _init(self, tmp_path, monkeypatch, country="SE", **env):
        import scripts.overpass_local_init as init
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", country)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(init, "DB_DIR", tmp_path / "db")
        monkeypatch.setattr(init, "META_DIR", tmp_path / "meta")
        (tmp_path / "db").mkdir(exist_ok=True)
        assert init.main() == 0
        return init, (tmp_path / "meta" / "start.sh").read_text(encoding="utf-8")

    def _cache(self, tmp_path, region="europe_sweden"):
        d = tmp_path / "db" / cfg.CACHE_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d / f"planet-{region}.osm.pbf"

    def test_launcher_prefers_the_cache_over_a_download(self, tmp_path, monkeypatch):
        # The decision lives in the launcher, not in the init script: the init
        # container runs once per `up`, but the sidecar restarts on failure —
        # and it is those restarts that must not hit the mirror again.
        _, script = self._init(tmp_path, monkeypatch)
        assert 'if [ -s "$OVERPASS_EXTRACT_CACHE" ]; then' in script
        assert 'export OVERPASS_PLANET_URL="file://$OVERPASS_EXTRACT_CACHE"' in script
        assert "download.geofabrik.de" in script

    def test_cache_survives_a_rerun_of_the_same_region(self, tmp_path, monkeypatch):
        cache = self._cache(tmp_path)
        cache.write_bytes(b"pbf")
        self._init(tmp_path, monkeypatch)
        assert cache.is_file()

    def test_cache_is_dropped_once_the_import_finished(self, tmp_path, monkeypatch):
        cache = self._cache(tmp_path)
        cache.write_bytes(b"pbf")
        (tmp_path / "db" / "init_done").write_text("")
        (tmp_path / "meta").mkdir(exist_ok=True)
        (tmp_path / "meta" / "region.txt").write_text("europe/sweden", encoding="utf-8")
        self._init(tmp_path, monkeypatch)
        assert not cache.exists()

    def test_cache_for_another_region_is_dropped(self, tmp_path, monkeypatch):
        stale = self._cache(tmp_path, region="europe_norway")
        stale.write_bytes(b"pbf")
        self._init(tmp_path, monkeypatch)
        assert not stale.exists()

    def test_a_cached_extract_is_not_a_database(self, tmp_path, monkeypatch):
        # _db_is_populated() drives the wipe decision. If the cache counted,
        # every first run would look like it already had a database.
        import scripts.overpass_local_init as init
        monkeypatch.setattr(init, "DB_DIR", tmp_path / "db")
        self._cache(tmp_path).write_bytes(b"pbf")
        assert not init._db_is_populated()
        (tmp_path / "db" / "init_done").write_text("")
        assert init._db_is_populated()

    def test_update_sleep_default_reaches_the_launcher(self, tmp_path, monkeypatch):
        _, script = self._init(tmp_path, monkeypatch)
        assert f': "${{OVERPASS_UPDATE_SLEEP:={7 * 24 * 3600}}}"' in script
