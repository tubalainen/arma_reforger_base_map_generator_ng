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
import httpx

from services import overpass_local as svc
from services import overpass_extract_converter as conv
from services import overpass_extract_fetcher as fetcher
from services import overpass_replication as replication
from services.osm_service import _pool_with_local

# Smallest thing that passes both of the fetcher's checks: the PBF magic in the
# first 64 bytes, and over MIN_PLAUSIBLE_BYTES in total.
FAKE_PBF = b"\x00\x00\x00\x0d\n\tOSMHeader\x18" + b"x" * (200 * 1024)

# Kept before the no-network guard swaps the class out, so the fetcher's own
# tests can still build a client over a mock transport.
_REAL_HTTPX_CLIENT = httpx.Client

# Just the header, for the shell-level guard which only inspects the first
# 64 bytes and does not care about size.
VALID_PBF_HEAD = FAKE_PBF[:32]


@pytest.fixture(autouse=True)
def no_real_downloads(monkeypatch):
    """No test may pull an extract from a real mirror.

    This is not hygiene, it is the same failure this module exists to prevent:
    a suite that downloads gigabytes on every CI run hammers the mirrors just
    as effectively as a restart loop does. Both the init step's fetch and the
    HTTP client underneath it are replaced, so a future test that forgets to
    stub gets an immediate assertion rather than a real transfer.
    """
    import scripts.overpass_local_init as init

    def _stub_fetch(url, destination, expected_gb=0.0, on_progress=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(FAKE_PBF)
        return fetcher.FetchOutcome(
            ok=True, reason="stubbed fetch", path=destination, downloaded_bytes=0
        )

    monkeypatch.setattr(init, "fetch_extract", _stub_fetch)

    def _refuse(*args, **kwargs):
        raise AssertionError(
            "A test tried to open a real HTTP connection. Use the "
            "`mock_mirror` fixture instead."
        )

    monkeypatch.setattr(fetcher.httpx, "Client", _refuse)
    monkeypatch.setattr(replication.httpx, "Client", _refuse)

    # Stub the init-level wrapper rather than replication.seed itself, so the
    # seeder's own tests still exercise the real thing over a mock transport.
    monkeypatch.setattr(init, "_seed_replication", lambda *a, **k: None)


@pytest.fixture
def mock_mirror(monkeypatch):
    """Serve the fetcher a scripted mirror instead of the network.

    Returns a setter taking an httpx.MockTransport handler.
    """

    def install(handler):
        def _client(**kwargs):
            kwargs.pop("transport", None)
            return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(fetcher.httpx, "Client", _client)

    return install


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
        # The planet file is fetched by the init step and handed over as a
        # local path; only the diff stream stays remote.
        script = self._launcher(tmp_path, monkeypatch)
        assert "planet-europe_sweden.osm.pbf" in script
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

    def test_a_valid_pbf_is_converted(self, tmp_path):
        # Caching moved into the fetcher, where the budget lives; this
        # script only guards and converts now.
        rc, out = self._run(tmp_path, VALID_PBF_HEAD)
        assert rc == 0, out


class TestExtractPlacement:
    """Where the extract lives, and who is allowed to fetch it."""

    def _init(self, tmp_path, monkeypatch, country="SE", mount=True, **env):
        import scripts.overpass_local_init as init
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", country)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(init, "DB_DIR", tmp_path / "db")
        monkeypatch.setattr(init, "META_DIR", tmp_path / "meta")
        monkeypatch.setattr(init, "EXTRACT_DIR", tmp_path / "extract")
        # is_mount() is meaningless on a tmp dir, so state the answer directly.
        monkeypatch.setattr(init.Path, "is_mount", lambda self: mount, raising=False)
        (tmp_path / "db").mkdir(exist_ok=True)
        rc = init.main()
        launcher = tmp_path / "meta" / "start.sh"
        return rc, launcher.read_text(encoding="utf-8") if launcher.is_file() else ""

    def test_sidecar_is_never_given_a_mirror_url(self, tmp_path, monkeypatch):
        # The whole point. The Overpass container's entrypoint re-downloads
        # whenever there is no database, so it must not hold a URL it could
        # download from — then no restart of it can cost a byte.
        rc, script = self._init(tmp_path, monkeypatch)
        assert rc == 0
        planet_line = next(
            line for line in script.splitlines()
            if line.startswith("export OVERPASS_PLANET_URL=")
        )
        assert planet_line.startswith("export OVERPASS_PLANET_URL='file://")
        assert "geofabrik" not in planet_line
        assert "openstreetmap.fr" not in planet_line
        # The diff URL stays remote by necessity — that is a weekly sweep of
        # small files, not a gigabyte re-download.
        assert "OVERPASS_DIFF_URL='https://" in script

    def test_extract_lands_on_its_own_volume(self, tmp_path, monkeypatch):
        # Separate from the database volume on purpose: wiping the database is
        # the documented recovery step and must not cost another download.
        rc, script = self._init(tmp_path, monkeypatch)
        assert rc == 0
        assert (tmp_path / "extract" / "planet-europe_sweden.osm.pbf").is_file()
        assert not (tmp_path / "db" / "extract_cache").exists()

    def test_missing_mount_falls_back_and_warns(self, tmp_path, monkeypatch, capsys):
        # Compose files predating the volume must keep working.
        rc, _ = self._init(tmp_path, monkeypatch, mount=False)
        assert rc == 0
        cached = tmp_path / "db" / "extract_cache" / "planet-europe_sweden.osm.pbf"
        assert cached.is_file()
        assert "no volume mounted" in capsys.readouterr().out

    def test_a_built_database_needs_no_extract(self, tmp_path, monkeypatch):
        (tmp_path / "db").mkdir(exist_ok=True)
        (tmp_path / "db" / "init_done").write_text("")
        rc, _ = self._init(tmp_path, monkeypatch)
        assert rc == 0
        assert not (tmp_path / "extract" / "planet-europe_sweden.osm.pbf").exists()

    def test_operator_file_url_is_passed_through_not_copied(self, tmp_path, monkeypatch):
        rc, script = self._init(
            tmp_path, monkeypatch, OVERPASS_PLANET_URL="file:///db/mine.osm.pbf"
        )
        assert rc == 0
        assert "export OVERPASS_PLANET_URL='file:///db/mine.osm.pbf'" in script
        assert not (tmp_path / "extract" / "planet-europe_sweden.osm.pbf").exists()

    def test_a_failed_fetch_stops_the_sidecar_starting(self, tmp_path, monkeypatch):
        # Exit non-zero -> `depends_on: service_completed_successfully` means
        # overpass-local never runs. One clear error instead of a restart loop.
        import scripts.overpass_local_init as init
        monkeypatch.setattr(
            init,
            "fetch_extract",
            lambda *a, **k: fetcher.FetchOutcome(ok=False, reason="mirror blocked"),
        )
        rc, _ = self._init(tmp_path, monkeypatch)
        assert rc == 1

    def test_the_partial_download_is_not_pruned(self, tmp_path, monkeypatch):
        # Pruning it would throw away everything already transferred and make
        # the next attempt start from zero.
        d = tmp_path / "extract"
        d.mkdir(parents=True)
        part = d / "planet-europe_sweden.osm.pbf.part"
        part.write_bytes(b"half a file")
        (d / cfg.LEDGER_NAME).write_text("{}", encoding="utf-8")
        (d / "planet-europe_norway.osm.pbf").write_bytes(b"wrong region")
        self._init(tmp_path, monkeypatch)
        assert part.is_file()
        assert (d / cfg.LEDGER_NAME).is_file()
        assert not (d / "planet-europe_norway.osm.pbf").exists()


class TestDownloadBudget:
    """The backstop for when the fetch itself keeps failing.

    A deployment of this app pulled 300+ GB from Geofabrik and was firewalled.
    Every limit here exists to make a repeat arithmetically impossible.
    """

    def _dest(self, tmp_path):
        return tmp_path / "extract" / "planet-europe_sweden.osm.pbf"

    def _serve(self, mock_mirror, body, status=200, headers=None):
        def handler(request):
            return httpx.Response(status, content=body, headers=headers or {})
        mock_mirror(handler)

    def test_a_present_extract_is_never_refetched(self, tmp_path, mock_mirror):
        dest = self._dest(tmp_path)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(FAKE_PBF)

        def handler(request):
            raise AssertionError("should not have contacted the mirror")
        mock_mirror(handler)

        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest)
        assert out.ok and out.already_present
        assert out.downloaded_bytes == 0

    def test_a_good_download_is_stored(self, tmp_path, mock_mirror):
        self._serve(mock_mirror, FAKE_PBF)
        dest = self._dest(tmp_path)
        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
        assert out.ok, out.reason
        assert dest.read_bytes() == FAKE_PBF

    def test_an_error_page_is_rejected_and_discarded(self, tmp_path, mock_mirror):
        self._serve(mock_mirror, b"<html>403 Forbidden</html>")
        dest = self._dest(tmp_path)
        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
        assert not out.ok
        assert not dest.exists()
        # Keeping it would mean a later resume appends real PBF data to HTML.
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    def test_attempts_stop_after_the_cap(self, tmp_path, mock_mirror):
        self._serve(mock_mirror, b"nope", status=500)
        dest = self._dest(tmp_path)
        for _ in range(cfg.MAX_DOWNLOAD_ATTEMPTS):
            out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
            assert not out.ok
            assert not out.budget_exhausted

        def handler(request):
            raise AssertionError("must not contact the mirror past the cap")
        mock_mirror(handler)

        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
        assert not out.ok and out.budget_exhausted
        assert out.retry_after is not None

    def test_the_cap_survives_container_recreation(self, tmp_path, mock_mirror):
        # The ledger is on the volume, not in the container. This is the case
        # that actually caused the 300 GB: re-running `docker compose up`
        # resets anything a container was counting.
        self._serve(mock_mirror, b"nope", status=500)
        dest = self._dest(tmp_path)
        for _ in range(cfg.MAX_DOWNLOAD_ATTEMPTS):
            fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)

        ledger = dest.parent / cfg.LEDGER_NAME
        assert ledger.is_file()
        reloaded = fetcher._Ledger.load(ledger)
        assert len(reloaded.recent_failures()) >= cfg.MAX_DOWNLOAD_ATTEMPTS

    def test_byte_budget_stops_a_server_that_ignores_resume(self, tmp_path, mock_mirror):
        # Transfers resume, so blowing through several times the file size means
        # the whole thing is being re-sent. Stop rather than keep paying for it.
        self._serve(mock_mirror, b"x" * (2 * 1024 * 1024))
        dest = self._dest(tmp_path)
        tiny_gb = 0.000001  # a budget of roughly 3 KB
        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, tiny_gb)
        assert not out.ok and out.budget_exhausted

    def test_a_partial_transfer_resumes(self, tmp_path, mock_mirror):
        dest = self._dest(tmp_path)
        dest.parent.mkdir(parents=True)
        part = dest.with_suffix(dest.suffix + ".part")
        part.write_bytes(FAKE_PBF[:1000])

        seen = {}
        total = len(FAKE_PBF)

        def handler(request):
            seen["range"] = request.headers.get("range")
            return httpx.Response(
                206,
                content=FAKE_PBF[1000:],
                headers={"content-range": f"bytes 1000-{total - 1}/{total}"},
            )

        mock_mirror(handler)
        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
        assert out.ok, out.reason
        assert seen["range"] == "bytes=1000-"
        assert dest.read_bytes() == FAKE_PBF
        # Only the missing part crossed the wire.
        assert out.downloaded_bytes == total - 1000

    def test_a_mirror_ignoring_range_starts_over_cleanly(self, tmp_path, mock_mirror):
        dest = self._dest(tmp_path)
        dest.parent.mkdir(parents=True)
        dest.with_suffix(dest.suffix + ".part").write_bytes(b"stale bytes")
        self._serve(mock_mirror, FAKE_PBF, status=200)
        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
        assert out.ok, out.reason
        # Appending to the stale partial would have corrupted the result.
        assert dest.read_bytes() == FAKE_PBF

    def test_requests_identify_the_app(self, tmp_path, mock_mirror):
        # A mirror that can see who we are can email us instead of firewalling.
        seen = {}

        def handler(request):
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, content=FAKE_PBF)

        mock_mirror(handler)
        fetcher.fetch_extract(
            "https://mirror.test/x.osm.pbf", self._dest(tmp_path), 0.001
        )
        assert "ArmaReforgerBaseMapGenerator" in seen["ua"]
        assert "github.com" in seen["ua"]

    def test_a_second_fetcher_does_not_start_a_parallel_transfer(
        self, tmp_path, mock_mirror
    ):
        dest = self._dest(tmp_path)
        dest.parent.mkdir(parents=True)
        (dest.parent / ".download.lock").write_text("999")

        def handler(request):
            raise AssertionError("must not transfer while another holds the lock")

        mock_mirror(handler)
        out = fetcher.fetch_extract("https://mirror.test/x.osm.pbf", dest, 0.001)
        assert not out.ok
        assert "Another container" in out.reason

    def test_a_404_names_the_likely_config_mistake(self, tmp_path, mock_mirror):
        self._serve(mock_mirror, b"missing", status=404)
        out = fetcher.fetch_extract(
            "https://mirror.test/x.osm.pbf", self._dest(tmp_path), 0.001
        )
        assert not out.ok
        assert "OVERPASS_LOCAL_MIRROR" in out.reason

    def test_a_429_says_do_not_retry(self, tmp_path, mock_mirror):
        self._serve(mock_mirror, b"slow down", status=429)
        out = fetcher.fetch_extract(
            "https://mirror.test/x.osm.pbf", self._dest(tmp_path), 0.001
        )
        assert not out.ok
        assert "Wait rather than" in out.reason


class TestConversionPlacement:
    """Where the PBF -> bzip2-XML conversion runs.

    In the sidecar it is single-threaded, because the Overpass image ships
    bzip2 but no parallel implementation — an hour on one core for Sweden while
    the other nineteen idle. Our own image has pbzip2, so it converts there
    when it can, and keeps the result so a failed import never pays twice.
    """

    def _init(self, tmp_path, monkeypatch, converts=True, fails=False):
        import scripts.overpass_local_init as init

        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setattr(init, "DB_DIR", tmp_path / "db")
        monkeypatch.setattr(init, "META_DIR", tmp_path / "meta")
        monkeypatch.setattr(init, "EXTRACT_DIR", tmp_path / "extract")
        monkeypatch.setattr(init.Path, "is_mount", lambda self: True, raising=False)
        (tmp_path / "db").mkdir(exist_ok=True)

        monkeypatch.setattr(conv, "available", lambda: converts)

        def _fake_convert(pbf, destination, threads=None):
            if fails:
                return conv.ConversionOutcome(ok=False, reason="boom")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"BZh9" + b"z" * 4096)
            return conv.ConversionOutcome(
                ok=True, reason="converted", path=destination, threads=10
            )

        monkeypatch.setattr(conv, "convert", _fake_convert)
        rc = init.main()
        launcher = tmp_path / "meta" / "start.sh"
        pre = tmp_path / "meta" / "preprocess.sh"
        return (
            rc,
            launcher.read_text(encoding="utf-8") if launcher.is_file() else "",
            pre.read_text(encoding="utf-8") if pre.is_file() else "",
        )

    def test_converted_here_the_sidecar_only_guards(self, tmp_path, monkeypatch):
        rc, script, pre = self._init(tmp_path, monkeypatch)
        assert rc == 0
        assert "planet-europe_sweden.osm.bz2" in script
        assert "osmium cat" not in pre
        assert "importing directly" in pre

    def test_the_pbf_is_dropped_once_converted(self, tmp_path, monkeypatch):
        # It has done its job; the archive is what a retry would reuse.
        self._init(tmp_path, monkeypatch)
        d = tmp_path / "extract"
        assert not (d / "planet-europe_sweden.osm.pbf").exists()
        assert (d / "planet-europe_sweden.osm.bz2").is_file()

    def test_an_existing_archive_skips_the_download_entirely(
        self, tmp_path, monkeypatch
    ):
        # The regression this guards: dropping the PBF after converting means
        # the next run finds no PBF, and would re-fetch it from the mirror if
        # it only ever looked for one.
        import scripts.overpass_local_init as init

        d = tmp_path / "extract"
        d.mkdir(parents=True)
        (d / "planet-europe_sweden.osm.bz2").write_bytes(b"BZh9" + b"z" * 4096)

        def _must_not_fetch(*args, **kwargs):
            raise AssertionError("re-downloaded an extract that was already converted")

        monkeypatch.setattr(init, "fetch_extract", _must_not_fetch)
        rc, script, pre = self._init(tmp_path, monkeypatch)
        assert rc == 0
        assert "planet-europe_sweden.osm.bz2" in script
        assert "importing directly" in pre

    def test_without_the_tools_the_sidecar_converts_as_before(
        self, tmp_path, monkeypatch
    ):
        rc, script, pre = self._init(tmp_path, monkeypatch, converts=False)
        assert rc == 0
        assert "planet-europe_sweden.osm.pbf" in script
        assert "osmium cat" in pre

    def test_a_failed_conversion_falls_back_rather_than_failing(
        self, tmp_path, monkeypatch
    ):
        # Slower, but a sidecar that comes up late beats one that never does.
        rc, script, pre = self._init(tmp_path, monkeypatch, fails=True)
        assert rc == 0
        assert "planet-europe_sweden.osm.pbf" in script
        assert "osmium cat" in pre


class TestConverter:
    def test_threads_default_to_half_the_machine(self, monkeypatch):
        # The init container usually shares a box with whatever else the
        # operator runs; pinning every core for ten minutes is its own rudeness.
        monkeypatch.delenv("OVERPASS_CONVERT_THREADS", raising=False)
        monkeypatch.setattr(conv.os, "cpu_count", lambda: 20)
        assert conv.default_threads() == 10

    def test_threads_never_drop_below_one(self, monkeypatch):
        monkeypatch.delenv("OVERPASS_CONVERT_THREADS", raising=False)
        monkeypatch.setattr(conv.os, "cpu_count", lambda: 1)
        assert conv.default_threads() == 1

    def test_operator_thread_count_wins(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_CONVERT_THREADS", "18")
        assert conv.default_threads() == 18

    def test_garbage_thread_count_falls_back(self, monkeypatch):
        monkeypatch.setenv("OVERPASS_CONVERT_THREADS", "lots")
        monkeypatch.setattr(conv.os, "cpu_count", lambda: 8)
        assert conv.default_threads() == 4

    def test_unavailable_without_both_tools(self, monkeypatch):
        monkeypatch.setattr(conv.shutil, "which", lambda n: None)
        assert not conv.available()
        monkeypatch.setattr(conv.shutil, "which", lambda n: "/usr/bin/" + n)
        assert conv.available()

    def test_the_pipeline_gives_the_threads_to_the_compressor(
        self, tmp_path, monkeypatch
    ):
        # osmium's XML generation is serial but cheap; bzip2 is the expensive
        # half, so it is the half that must be parallel.
        monkeypatch.setattr(conv.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setenv("OVERPASS_CONVERT_THREADS", "12")
        seen = []

        class FakeProc:
            returncode = 0

            def __init__(self, argv, **kwargs):
                seen.append(argv)
                self.stdout = kwargs.get("stdout")
                if hasattr(self.stdout, "write"):
                    self.stdout.write(b"BZh9" + b"z" * 4096)
                    self.stdout = None
                else:
                    self.stdout = _ClosablePipe()

            def communicate(self):
                return b"", b""

        class _ClosablePipe:
            def close(self):
                pass

        monkeypatch.setattr(conv.subprocess, "Popen", FakeProc)
        pbf = tmp_path / "in.osm.pbf"
        pbf.write_bytes(b"\x00\x00\x00\x0d\n\tOSMHeader")
        out = conv.convert(pbf, tmp_path / "out.osm.bz2")

        assert out.ok, out.reason
        reader, writer = seen
        assert reader[:2] == ["osmium", "cat"]
        assert "-o" in reader and "-" in reader
        assert writer[0] == "pbzip2"
        assert "-p12" in writer

    def test_a_truncated_conversion_is_not_left_looking_finished(
        self, tmp_path, monkeypatch
    ):
        # An import handed a truncated archive fails an hour later for the
        # wrong reason, so a bad conversion must leave nothing behind.
        monkeypatch.setattr(conv.shutil, "which", lambda n: "/usr/bin/" + n)

        class FakeProc:
            returncode = 0

            def __init__(self, argv, **kwargs):
                self.stdout = kwargs.get("stdout")
                if hasattr(self.stdout, "write"):
                    self.stdout.write(b"not bzip2 at all")
                    self.stdout = None
                else:
                    self.stdout = type("P", (), {"close": lambda s: None})()

            def communicate(self):
                return b"", b""

        monkeypatch.setattr(conv.subprocess, "Popen", FakeProc)
        pbf = tmp_path / "in.osm.pbf"
        pbf.write_bytes(b"\x00\x00\x00\x0d\n\tOSMHeader")
        dest = tmp_path / "out.osm.bz2"
        out = conv.convert(pbf, dest)

        assert not out.ok
        assert not dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()

    def test_an_existing_archive_is_reused(self, tmp_path, monkeypatch):
        def _must_not_run(*a, **k):
            raise AssertionError("reconverted an extract that was already converted")

        monkeypatch.setattr(conv.subprocess, "Popen", _must_not_run)
        dest = tmp_path / "out.osm.bz2"
        dest.write_bytes(b"BZh9" + b"z" * 4096)
        out = conv.convert(tmp_path / "in.osm.pbf", dest)
        assert out.ok and out.already_present


@pytest.fixture
def mock_replication(monkeypatch):
    """Serve the replication seeder a scripted state server."""

    def install(handler):
        def _client(**kwargs):
            kwargs.pop("transport", None)
            return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(replication.httpx, "Client", _client)

    return install


def _state_body(seq, iso):
    """An osmosis state file. Colons in the timestamp come back escaped,
    because the format is a Java properties file."""
    escaped = iso.replace(":", "\\:")
    return "#comment\nsequenceNumber=%d\ntimestamp=%s\n" % (seq, escaped)


class TestDbPermissions:
    """The Overpass image runs its query CGI as `nginx` but owns /db as
    `overpass`, and Debian bookworm's adduser makes home directories 0700.
    The result is a database that imports perfectly and then refuses every
    query with `open64: 13 Permission denied /db/db//osm3s_osm_base`."""

    def _run(self, monkeypatch, mode):
        import scripts.overpass_local_init as init

        applied = []

        class FakeStat:
            st_mode = 0o040000 | mode

        class FakeDir:
            def stat(self):
                return FakeStat()

            def chmod(self, new):
                applied.append(new)

        monkeypatch.setattr(init, "DB_DIR", FakeDir())
        init._ensure_db_traversable()
        return applied

    def test_a_private_db_directory_is_opened_up(self, monkeypatch):
        assert self._run(monkeypatch, 0o700) == [0o755]

    def test_an_already_traversable_directory_is_left_alone(self, monkeypatch):
        assert self._run(monkeypatch, 0o755) == []

    def test_only_read_and_traverse_are_added(self, monkeypatch):
        # Never write: other users have no business modifying the database.
        applied = self._run(monkeypatch, 0o750)[0]
        assert applied == 0o755
        assert not applied & 0o022


class TestReplicationSeeding:
    """Without /db/replicate_id the diff loop cannot start and cannot create
    the file either, so the sidecar serves data that ages forever in silence."""

    def test_an_existing_sequence_file_is_never_touched(self, tmp_path):
        # Once the sidecar is running it owns this file; rewriting it would
        # rewind or skip the update stream.
        seq = tmp_path / "replicate_id"
        seq.write_text("12345\n")
        outcome = replication.seed(seq, "https://example.org/updates/", None)
        assert outcome.ok and outcome.already_present
        assert seq.read_text() == "12345\n"

    def test_missing_timestamp_is_reported_not_guessed(self, tmp_path):
        outcome = replication.seed(
            tmp_path / "replicate_id", "https://example.org/updates/", None
        )
        assert not outcome.ok
        assert "starting point" in outcome.reason

    def test_no_diff_url_means_nothing_to_seed(self, tmp_path):
        outcome = replication.seed(tmp_path / "replicate_id", "", None)
        assert outcome.ok and outcome.sequence is None

    def test_state_path_uses_the_osmosis_layout(self):
        assert replication._state_path(7256890) == "007/256/890.state.txt"
        assert replication._state_path(1) == "000/000/001.state.txt"

    def test_escaped_timestamps_parse(self):
        seq, stamp = replication._parse_state(
            _state_body(7256888, "2026-08-24T14:55:21Z")
        )
        assert seq == 7256888
        assert stamp.year == 2026 and stamp.hour == 14

    def _days(self):
        return {n: "2026-08-%02dT20:00:00Z" % (10 + n) for n in range(1, 11)}

    def _server(self, newest, per_seq):
        def handler(request):
            path = request.url.path
            if path.endswith("/state.txt") and path.count("/") <= 2:
                return httpx.Response(200, text=_state_body(*newest))
            for seq, iso in per_seq.items():
                if path.endswith(replication._state_path(seq)):
                    return httpx.Response(200, text=_state_body(seq, iso))
            return httpx.Response(404)

        return handler

    def test_resolves_the_sequence_at_or_before_the_target(
        self, mock_replication, monkeypatch
    ):
        from datetime import datetime, timezone

        days = self._days()
        mock_replication(self._server((10, days[10]), days))
        target = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        # Sequence 5 is 2026-08-15T20:00, after the target; 4 is 08-14T20:00.
        assert replication.sequence_for_timestamp("https://x.test/u/", target) == 4

    def test_resuming_early_is_preferred_to_resuming_late(
        self, mock_replication, monkeypatch
    ):
        # Re-applying changes the database already has is harmless. Skipping
        # changes leaves a permanent hole.
        from datetime import datetime, timezone

        days = self._days()
        mock_replication(self._server((10, days[10]), days))
        target = datetime(2026, 8, 15, 19, 59, tzinfo=timezone.utc)
        assert replication.sequence_for_timestamp("https://x.test/u/", target) == 4

    def test_a_target_newer_than_the_server_takes_the_head(
        self, mock_replication, monkeypatch
    ):
        from datetime import datetime, timezone

        days = self._days()
        mock_replication(self._server((10, days[10]), days))
        target = datetime(2027, 1, 1, tzinfo=timezone.utc)
        assert replication.sequence_for_timestamp("https://x.test/u/", target) == 10

    def test_probes_are_bounded(self, mock_replication, monkeypatch):
        # A server laid out differently than we assume must not turn into an
        # unbounded crawl against someone else's infrastructure.
        from datetime import datetime, timezone

        seen = []

        def handler(request):
            seen.append(request.url.path)
            path = request.url.path
            if path.endswith("/state.txt") and path.count("/") <= 2:
                return httpx.Response(
                    200, text=_state_body(9000000, "2026-08-24T00:00:00Z")
                )
            return httpx.Response(500)

        mock_replication(handler)
        replication.sequence_for_timestamp(
            "https://x.test/u/", datetime(2020, 1, 1, tzinfo=timezone.utc)
        )
        assert len(seen) <= replication.MAX_STATE_PROBES

    def test_an_unreachable_server_returns_none_rather_than_raising(
        self, mock_replication, monkeypatch
    ):
        from datetime import datetime, timezone

        def handler(request):
            raise httpx.ConnectError("blocked")

        mock_replication(handler)
        assert (
            replication.sequence_for_timestamp(
                "https://x.test/u/", datetime(2026, 1, 1, tzinfo=timezone.utc)
            )
            is None
        )


class TestSeedingIsWiredIn:
    def test_init_seeds_while_the_pbf_still_exists(self, tmp_path, monkeypatch):
        # The replication headers live in the PBF, do not survive conversion,
        # and the PBF is deleted once converted — so ordering is the fix.
        import scripts.overpass_local_init as init

        saw = {}

        def _spy(diffs, extract_file, extracts):
            saw["diffs"] = diffs
            saw["pbf_present"] = extract_file is not None and extract_file.is_file()

        monkeypatch.setattr(init, "_seed_replication", _spy)
        monkeypatch.setenv("OVERPASS_LOCAL_COUNTRIES", "SE")
        monkeypatch.setattr(init, "DB_DIR", tmp_path / "db")
        monkeypatch.setattr(init, "META_DIR", tmp_path / "meta")
        monkeypatch.setattr(init, "EXTRACT_DIR", tmp_path / "extract")
        monkeypatch.setattr(init.Path, "is_mount", lambda self: True, raising=False)
        (tmp_path / "db").mkdir()

        assert init.main() == 0
        assert saw["pbf_present"] is True
        assert saw["diffs"].endswith("sweden-updates/")


class TestSeededFileOwnership:
    """This container runs as root; the sidecar's update loop runs as
    `overpass` and *rewrites* the sequence file after every batch. A
    root-owned replicate_id is readable but not advanceable, so the loop
    re-downloads the same diffs on every cycle, forever."""

    def _seed(self, tmp_path, monkeypatch, sequence=4242, ok=True):
        import scripts.overpass_local_init as init

        monkeypatch.undo()  # drop the autouse stub of _seed_replication
        chowned = []

        db = tmp_path / "db"
        db.mkdir()
        monkeypatch.setattr(init, "DB_DIR", db)

        def _fake_seed(sequence_file, diff_url, target):
            if ok:
                sequence_file.write_text("%d\n" % sequence)
                return replication.SeedOutcome(
                    ok=True, sequence=sequence, reason="seeded"
                )
            return replication.SeedOutcome(ok=False, reason="mirror unreachable")

        monkeypatch.setattr(replication, "seed", _fake_seed)
        # raising=False both creates the attribute on Windows dev hosts and
        # satisfies the hasattr() guard, so the real code path is exercised.
        monkeypatch.setattr(
            init.os,
            "chown",
            lambda p, u, g: chowned.append((str(p), u, g)),
            raising=False,
        )
        init._seed_replication("https://x.test/u/", None, tmp_path / "extract")
        return chowned, db

    def test_the_sequence_file_is_handed_to_the_db_owner(self, tmp_path, monkeypatch):
        chowned, db = self._seed(tmp_path, monkeypatch)
        assert len(chowned) == 1
        path, uid, gid = chowned[0]
        assert path.endswith("replicate_id")
        # Taken from /db rather than hardcoded, so it survives the upstream
        # image renumbering its user.
        assert (uid, gid) == (db.stat().st_uid, db.stat().st_gid)

    def test_nothing_is_chowned_when_seeding_failed(self, tmp_path, monkeypatch):
        chowned, _ = self._seed(tmp_path, monkeypatch, ok=False)
        assert chowned == []
