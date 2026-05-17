"""
Tests for middleware/rate_limit.py.

The middleware previously did `raise HTTPException(429, ...)` from
`BaseHTTPMiddleware.dispatch`. Starlette does not run FastAPI's exception
handlers for exceptions raised inside middleware — the HTTPException flowed
up through an anyio ExceptionGroup and surfaced as a 500 Internal Server
Error, masking the real 429 from the client. The fix is to return a
JSONResponse directly. These tests pin that contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

# fastapi/starlette live in the Docker image but not on every dev machine.
pytest.importorskip("fastapi", reason="fastapi required for middleware tests")
pytest.importorskip("starlette", reason="starlette required for middleware tests")

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture
def app_with_low_limits(monkeypatch):
    """Build an isolated FastAPI app with tiny rate limits so the test can
    trip them in a couple of requests."""
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "3")
    monkeypatch.setenv("RATE_LIMIT_GENERATE_PER_HOUR", "2")

    # Force a fresh import so the module-level env-driven constants reload.
    for mod_name in [
        "middleware.rate_limit",
        "middleware",
    ]:
        sys.modules.pop(mod_name, None)
    from middleware.rate_limit import RateLimitMiddleware  # noqa: E402

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/generate")
    def generate():
        return {"ok": True}

    return app


class TestGeneralRateLimit:
    def test_under_limit_returns_200(self, app_with_low_limits):
        client = TestClient(app_with_low_limits)
        for _ in range(3):
            resp = client.get("/api/ping")
            assert resp.status_code == 200

    def test_over_limit_returns_429_not_500(self, app_with_low_limits):
        """The original bug: HTTPException raised in BaseHTTPMiddleware was
        surfaced as 500. The fix returns a proper 429 JSONResponse."""
        client = TestClient(app_with_low_limits)
        for _ in range(3):
            client.get("/api/ping")
        resp = client.get("/api/ping")
        assert resp.status_code == 429, (
            f"expected 429, got {resp.status_code} — the middleware is "
            f"raising HTTPException again and Starlette is masking it as 500"
        )

    def test_429_body_is_json_with_detail(self, app_with_low_limits):
        client = TestClient(app_with_low_limits)
        for _ in range(4):
            resp = client.get("/api/ping")
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert "detail" in body
        assert "Too many requests" in body["detail"]

    def test_429_includes_retry_after_and_ratelimit_headers(
        self, app_with_low_limits
    ):
        client = TestClient(app_with_low_limits)
        for _ in range(4):
            resp = client.get("/api/ping")
        assert resp.headers.get("Retry-After") == "60"
        assert resp.headers.get("X-RateLimit-Limit") == "3"
        assert resp.headers.get("X-RateLimit-Remaining") == "0"


class TestGenerateRateLimit:
    def test_generate_over_limit_returns_429_not_500(self, app_with_low_limits):
        client = TestClient(app_with_low_limits)
        # Two POSTs are allowed (GENERATE_PER_HOUR=2); the third trips it.
        client.post("/api/generate")
        client.post("/api/generate")
        resp = client.post("/api/generate")
        assert resp.status_code == 429
        body = resp.json()
        assert "Generation limit reached" in body["detail"]
        assert resp.headers.get("Retry-After") == "3600"


class TestNonApiPathsAreExempt:
    def test_health_endpoint_is_not_rate_limited(self, app_with_low_limits):
        # Mount a /api/health route too — exempt per the middleware.
        @app_with_low_limits.get("/api/health")
        def health():
            return {"ok": True}

        client = TestClient(app_with_low_limits)
        for _ in range(20):
            resp = client.get("/api/health")
            assert resp.status_code == 200

    def test_non_api_paths_are_not_rate_limited(self, app_with_low_limits):
        @app_with_low_limits.get("/static-asset")
        def asset():
            return {"ok": True}

        client = TestClient(app_with_low_limits)
        for _ in range(20):
            resp = client.get("/static-asset")
            assert resp.status_code == 200

    def test_preview_endpoints_are_not_rate_limited(self, app_with_low_limits):
        """Issue #119: the results-page <img> retry loop was tripping the
        general /api/* limit and starving previews into permanent 429s.
        Preview fetches must be exempt — session ownership is already
        enforced inside the route handler."""

        @app_with_low_limits.get("/api/job/{job_id}/preview/{image_type}")
        def preview(job_id: str, image_type: str):
            return {"ok": True}

        client = TestClient(app_with_low_limits)
        # Far more than REQUESTS_PER_MINUTE (=3 in this fixture) — none
        # should be throttled.
        for _ in range(20):
            resp = client.get("/api/job/abc/preview/heightmap")
            assert resp.status_code == 200
            resp = client.get("/api/job/abc/preview/surface")
            assert resp.status_code == 200
