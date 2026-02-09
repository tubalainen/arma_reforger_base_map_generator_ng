"""
Rate limiting middleware.

Provides application-level rate limiting as a backup to nginx/Cloudflare.
Uses a sliding window algorithm with separate limits for different endpoints.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Configuration from environment with sensible defaults
REQUESTS_PER_MINUTE = int(os.environ.get("RATE_LIMIT_REQUESTS_PER_MINUTE", "60"))
GENERATE_PER_HOUR = int(os.environ.get("RATE_LIMIT_GENERATE_PER_HOUR", "10"))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces rate limits per client IP.

    Features:
    - General rate limit for all requests
    - Stricter rate limit for /api/generate endpoint
    - Uses X-Real-IP or X-Forwarded-For if behind a proxy
    """

    def __init__(self, app):
        super().__init__(app)
        self.request_counts: dict[str, list[datetime]] = defaultdict(list)
        self.generate_counts: dict[str, list[datetime]] = defaultdict(list)

    def _get_client_ip(self, request: Request) -> str:
        """Extract the real client IP, handling proxy headers."""
        # Check for proxy headers first
        forwarded_for = request.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            # Take the first IP in the chain (original client)
            return forwarded_for.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        # Fall back to direct client IP
        if request.client:
            return request.client.host

        return "unknown"

    def _clean_old_requests(
        self, timestamps: list[datetime], window: timedelta
    ) -> list[datetime]:
        """Remove timestamps older than the window."""
        cutoff = datetime.utcnow() - window
        return [t for t in timestamps if t > cutoff]

    async def dispatch(self, request: Request, call_next) -> Response:
        client_ip = self._get_client_ip(request)
        now = datetime.utcnow()
        path = request.url.path

        # Skip rate limiting for health checks
        if path == "/api/health":
            return await call_next(request)

        # Clean old timestamps and check general rate limit
        self.request_counts[client_ip] = self._clean_old_requests(
            self.request_counts[client_ip], timedelta(minutes=1)
        )

        if len(self.request_counts[client_ip]) >= REQUESTS_PER_MINUTE:
            logger.warning(f"Rate limit exceeded for {client_ip}: general limit")
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down.",
            )

        # Special stricter rate limit for /api/generate
        if path == "/api/generate" and request.method == "POST":
            self.generate_counts[client_ip] = self._clean_old_requests(
                self.generate_counts[client_ip], timedelta(hours=1)
            )

            if len(self.generate_counts[client_ip]) >= GENERATE_PER_HOUR:
                logger.warning(f"Rate limit exceeded for {client_ip}: generate limit")
                raise HTTPException(
                    status_code=429,
                    detail=f"Generation limit reached ({GENERATE_PER_HOUR}/hour). Please try again later.",
                )

            self.generate_counts[client_ip].append(now)

        # Record this request
        self.request_counts[client_ip].append(now)

        # Add rate limit headers to response
        response = await call_next(request)
        remaining = REQUESTS_PER_MINUTE - len(self.request_counts[client_ip])
        response.headers["X-RateLimit-Limit"] = str(REQUESTS_PER_MINUTE)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))

        return response
