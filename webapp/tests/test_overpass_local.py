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
        assert "daily diffs" in status["message"]

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
        (tmp_path / "db").mkdir()
        assert init.main() == 0
        return (tmp_path / "meta" / "start.sh").read_text(encoding="utf-8")

    def test_launcher_sets_the_resolved_urls(self, tmp_path, monkeypatch):
        script = self._launcher(tmp_path, monkeypatch)
        assert "europe/sweden-latest.osm.pbf" in script
        assert "europe/sweden-updates/" in script

    def test_launcher_carries_the_pbf_conversion(self, tmp_path, monkeypatch):
        # Without this the importer's `bunzip2 < planet.osm.bz2` is handed a
        # PBF and the whole import dies — the v1.10.0 failure.
        script = self._launcher(tmp_path, monkeypatch)
        assert "osmium cat" in script
        assert "/db/planet.osm.bz2" in script

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
