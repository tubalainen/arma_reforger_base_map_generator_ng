"""Lantmäteriet API authentication.

Provides Basic Authentication helpers for Lantmäteriet's STAC and WMS APIs.
Credentials are read from environment variables via config/lantmateriet.py.

Usage:
    from services.lantmateriet.auth import get_authenticated_headers
    headers = get_authenticated_headers()
    resp = await client.get(url, headers=headers)
"""

import base64
import logging
from typing import Optional

from config.lantmateriet import LANTMATERIET_CONFIG

logger = logging.getLogger(__name__)


def get_basic_auth_header() -> Optional[dict]:
    """
    Generate Basic Authentication header for Lantmäteriet APIs.

    Returns:
        Dict with Authorization header, or None if credentials missing.
    """
    if not LANTMATERIET_CONFIG.has_credentials():
        logger.warning(
            "Lantmäteriet credentials not configured. "
            "Set LANTMATERIET_USERNAME and LANTMATERIET_PASSWORD in .env file. "
            "Register at https://apimanager.lantmateriet.se/"
        )
        return None

    credentials = (
        f"{LANTMATERIET_CONFIG.username}:"
        f"{LANTMATERIET_CONFIG.password}"
    )
    encoded = base64.b64encode(credentials.encode()).decode()

    return {"Authorization": f"Basic {encoded}"}


def get_authenticated_headers() -> dict:
    """
    Get headers with authentication for Lantmäteriet API requests.

    Always includes User-Agent and Accept headers.
    Adds Authorization header if credentials are available.

    Returns:
        Dict with headers (including Authorization if credentials available).
    """
    headers = {
        "User-Agent": "ArmaReforgerMapGenerator/1.0",
        "Accept": "application/json",
    }

    auth_header = get_basic_auth_header()
    if auth_header:
        headers.update(auth_header)

    return headers
