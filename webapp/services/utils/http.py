"""
Async HTTP client utilities with retry and fallback logic.

Provides reusable retry-with-fallback-endpoints pattern used by
services that call external APIs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


async def async_request_with_retry(
    method: str,
    endpoints: list[str] | str,
    max_retries: int = 2,
    timeout: float = 210.0,
    retry_wait_s: float = 5.0,
    retryable_status_codes: tuple[int, ...] = (429, 502, 503, 504),
    user_agent: str = "ArmaReforgerMapGenerator/1.0",
    exponential_backoff: bool = True,
    **request_kwargs,
) -> Optional[httpx.Response]:
    """
    Make an HTTP request with retry logic across single or multiple endpoints.

    Tries each endpoint in order; on retryable status codes or connection
    errors, moves to the next endpoint. After exhausting all endpoints,
    waits and retries the full cycle.

    Args:
        method: HTTP method ("GET" or "POST")
        endpoints: Single URL string or list of base URLs to try in order
        max_retries: Number of full retry cycles
        timeout: Request timeout in seconds
        retry_wait_s: Seconds to wait between retry cycles (base wait time)
        retryable_status_codes: HTTP status codes that trigger retry (default: 429, 502, 503, 504)
        user_agent: User-Agent header value
        exponential_backoff: If True, wait time doubles with each retry attempt
        **request_kwargs: Additional kwargs passed to httpx request (data, params, headers, etc.)

    Returns:
        httpx.Response on success, or None if all attempts failed
    """
    # Support single endpoint as string
    if isinstance(endpoints, str):
        endpoints = [endpoints]

    headers = request_kwargs.pop("headers", {})
    headers.setdefault("User-Agent", user_agent)

    for attempt in range(max_retries):
        for endpoint in endpoints:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if method.upper() == "POST":
                        resp = await client.post(
                            endpoint, headers=headers, **request_kwargs
                        )
                    else:
                        resp = await client.get(
                            endpoint, headers=headers, **request_kwargs
                        )

                    if resp.status_code == 200:
                        return resp
                    elif resp.status_code in retryable_status_codes:
                        logger.warning(
                            f"Retryable status {resp.status_code} from {endpoint}, trying next..."
                        )
                        continue
                    else:
                        logger.error(
                            f"HTTP error {resp.status_code} from {endpoint}: "
                            f"{resp.text[:300]}"
                        )
                        continue
            except Exception as e:
                logger.error(f"Request failed on {endpoint}: {e}")
                continue

        # All endpoints failed on this attempt
        if attempt < max_retries - 1:
            # Calculate wait time with optional exponential backoff
            wait_time = retry_wait_s * (2 ** attempt if exponential_backoff else 1)
            logger.warning(
                f"All endpoints failed, retrying in {wait_time}s "
                f"(attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(wait_time)

    logger.error("All endpoints failed after all retries")
    return None
