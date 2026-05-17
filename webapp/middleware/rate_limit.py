"""
Rate limiting middleware.

Provides application-level rate limiting as a backup to nginx/Cloudflare.
Uses a sliding window algorithm with separate limits for different endpoints.

Rate-limit key: session ID (from cookie) when available, falling back to
client IP.  This ensures that parallel users behind the same NAT/proxy each
get their own rate-limit bucket instead of sharing a single IP-based one.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Configuration from environment with sensible defaults
REQUESTS_PER_MINUTE = int(os.environ.get("RATE_LIMIT_REQUESTS_PER_MINUTE", "60"))
GENERATE_PER_HOUR = int(os.environ.get("RATE_LIMIT_GENERATE_PER_HOUR", "10"))

# Session cookie name — must match the value in middleware/session.py
SESSION_COOKIE_NAME = "arma_session"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces rate limits per user session (or IP fallback).

    Features:
    - General rate limit for /api/* requests
    - Stricter rate limit for POST /api/generate
    - Uses session cookie for per-user isolation (no shared IP buckets)
    - Falls back to IP when no session cookie is present
    """

    def __init__(self, app):
        super().__init__(app)
        self.request_counts: dict[str, list[datetime]] = defaultdict(list)
        self.generate_counts: dict[str, list[datetime]] = defaultdict(list)

    def _get_rate_limit_key(self, request: Request) -> str:
        """
        Get the rate-limit bucket key for this request.

        Prefers session ID (from cookie) so that parallel users behind
        the same NAT/proxy get independent buckets.  Falls back to IP
        when no session cookie is present (e.g. first request before
        the session middleware sets the cookie).
        """
        # Try session cookie first
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if session_id:
            return f"session:{session_id}"

        # Fallback to IP
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            return f"ip:{forwarded_for.split(',')[0].strip()}"

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return f"ip:{real_ip}"

        if request.client:
            return f"ip:{request.client.host}"

        return "ip:unknown"

    def _clean_old_requests(
        self, timestamps: list[datetime], window: timedelta
    ) -> list[datetime]:
        """Remove timestamps older than the window."""
        cutoff = datetime.utcnow() - window
        return [t for t in timestamps if t > cutoff]

    def _rate_limited_response(
        self,
        detail: str,
        limit: int,
        retry_after_seconds: int,
    ) -> JSONResponse:
        """
        Build the 429 response.

        Important: we MUST return a Response from a BaseHTTPMiddleware.dispatch
        rather than `raise HTTPException(...)`. Exceptions raised inside a
        BaseHTTPMiddleware do not flow through FastAPI's exception handlers —
        Starlette wraps them in an anyio ExceptionGroup that surfaces as a
        500 Internal Server Error, hiding the real 429 from the client.
        """
        return JSONResponse(
            status_code=429,
            content={"detail": detail},
            headers={
                "Retry-After": str(retry_after_seconds),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Only rate-limit API endpoints.
        # Exempt everything else (static assets, status polling, downloads,
        # page loads) so that parallel users don't starve each other and
        # the frontend's 1.5s status polling doesn't eat into the budget.
        if not path.startswith("/api/"):
            return await call_next(request)

        # Health check is internal — never rate-limit it
        if path == "/api/health":
            return await call_next(request)

        # Preview images (heightmap / surface) are fetched by the results-page
        # <img> tags after a job completes. A failed load triggers an immediate
        # onerror retry with a fresh cache-buster, and on a 429 the browser
        # loops on the error path — a few hundred requests per second is
        # easily reachable and starves the user's whole /api/* budget so the
        # previews never paint (issue #119). Session ownership is already
        # enforced inside get_preview(); rate-limiting buys nothing here.
        if path.startswith("/api/job/") and "/preview/" in path:
            return await call_next(request)

        key = self._get_rate_limit_key(request)
        now = datetime.utcnow()

        # Clean old timestamps and check general rate limit
        self.request_counts[key] = self._clean_old_requests(
            self.request_counts[key], timedelta(minutes=1)
        )

        if len(self.request_counts[key]) >= REQUESTS_PER_MINUTE:
            logger.warning(f"Rate limit exceeded for {key}: general limit")
            return self._rate_limited_response(
                detail="Too many requests. Please slow down.",
                limit=REQUESTS_PER_MINUTE,
                retry_after_seconds=60,
            )

        # Special stricter rate limit for /api/generate
        if path == "/api/generate" and request.method == "POST":
            self.generate_counts[key] = self._clean_old_requests(
                self.generate_counts[key], timedelta(hours=1)
            )

            if len(self.generate_counts[key]) >= GENERATE_PER_HOUR:
                logger.warning(f"Rate limit exceeded for {key}: generate limit")
                return self._rate_limited_response(
                    detail=(
                        f"Generation limit reached ({GENERATE_PER_HOUR}/hour). "
                        f"Please try again later."
                    ),
                    limit=GENERATE_PER_HOUR,
                    retry_after_seconds=3600,
                )

            self.generate_counts[key].append(now)

        # Record this request
        self.request_counts[key].append(now)

        # Add rate limit headers to response
        response = await call_next(request)
        remaining = REQUESTS_PER_MINUTE - len(self.request_counts[key])
        response.headers["X-RateLimit-Limit"] = str(REQUESTS_PER_MINUTE)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))

        return response
