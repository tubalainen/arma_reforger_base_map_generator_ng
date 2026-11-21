"""Guard: the Lantmäteriet config must be read through its module, never bound.

`config/lantmateriet.py` holds one `LANTMATERIET_CONFIG` instance built from
the environment at import time, and reassigning that name is the only way the
config is ever reloaded - it is how the tests inject credentials, and the
reason `_load_config()` exists as a factory at all.

A module that does `from config.lantmateriet import LANTMATERIET_CONFIG` binds
the instance that existed when *it* was first imported, so the reassignment
never reaches it. `services/lantmateriet/auth.py` did exactly that, and the
cost was two test failures written off as "environment noise" for months
(fixed in v1.19.0) plus a third test that passed only because the stale
snapshot happened to be empty. These tests make the next occurrence fail loudly
instead.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import replace

import pytest

import config.lantmateriet as lm_config
import services.lantmateriet as lm_services
from config.lantmateriet import LantmaterietConfig


def _lantmateriet_modules():
    """Every module under services/lantmateriet, discovered rather than listed.

    Enumerated so a module added tomorrow is covered the day it is written.
    """
    mods = []
    for info in pkgutil.iter_modules(lm_services.__path__):
        mods.append(importlib.import_module(f"services.lantmateriet.{info.name}"))
    return mods


@pytest.fixture
def swapped_config():
    """Reassign the global config the way the app's only reload path does."""
    original = lm_config.LANTMATERIET_CONFIG

    def swap(**overrides):
        lm_config.LANTMATERIET_CONFIG = replace(original, **overrides)
        return lm_config.LANTMATERIET_CONFIG

    yield swap
    lm_config.LANTMATERIET_CONFIG = original


class TestNothingBindsTheConfigByValue:

    def test_the_modules_are_actually_discovered(self):
        """Positive assertion first: the guard below must have real input."""
        names = sorted(m.__name__.rsplit(".", 1)[-1] for m in _lantmateriet_modules())
        assert "auth" in names
        # auth + hydrografi + marktacke + ogc_features_client + orthophoto
        # + stac_elevation + stac_orthophoto_service
        assert len(names) >= 7, names

    def test_no_module_has_a_module_level_config_binding(self):
        offenders = [
            m.__name__ for m in _lantmateriet_modules()
            if "LANTMATERIET_CONFIG" in vars(m)
        ]
        assert not offenders, (
            "these modules bound LANTMATERIET_CONFIG at import time and will not "
            f"see a reloaded config: {offenders}. Import the module and read "
            "`lm_config.LANTMATERIET_CONFIG` at call time instead."
        )

    def test_the_modules_that_read_it_share_the_config_module(self):
        readers = [m for m in _lantmateriet_modules() if hasattr(m, "lm_config")]
        assert readers, "no module reads the config through its module at all"
        for m in readers:
            assert m.lm_config is lm_config, m.__name__


class TestAReloadedConfigIsVisible:
    """The functional half: a swapped config must reach the request itself."""

    @pytest.mark.asyncio
    async def test_wms_request_uses_the_reloaded_credentials_and_limits(
        self, swapped_config, monkeypatch
    ):
        from services.lantmateriet import orthophoto_service

        swapped_config(
            username="testuser",
            password="testpass",
            max_tile_size=7,
            orthophoto_wms="https://example.invalid/swapped-wms",
        )

        captured = {}

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "image/png"}
            content = b"PNG"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None, headers=None):
                captured["url"] = url
                captured["params"] = params
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr(orthophoto_service.httpx, "AsyncClient", FakeClient)

        result = await orthophoto_service.fetch_historical_orthophoto(
            (13.0, 58.0, 13.1, 58.1), width=4096, height=4096
        )

        assert result == b"PNG"
        # auth.py read the reloaded credentials...
        assert captured["headers"]["Authorization"].startswith("Basic ")
        # ...and orthophoto_service read the reloaded endpoint and tile cap.
        assert captured["url"] == "https://example.invalid/swapped-wms"
        assert captured["params"]["WIDTH"] == "7"
        assert captured["params"]["HEIGHT"] == "7"
