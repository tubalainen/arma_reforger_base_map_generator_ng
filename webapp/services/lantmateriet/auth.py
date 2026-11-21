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

# Imported as a module, and the attribute read inside each function, on
# purpose. `from ... import LANTMATERIET_CONFIG` binds the object that existed
# when this module was first imported, so reassigning
# `config.lantmateriet.LANTMATERIET_CONFIG` - which is the only way the config
# is ever reloaded - would never reach the credential check here.
from config import lantmateriet as lm_config

logger = logging.getLogger(__name__)


def get_basic_auth_header() -> Optional[dict]:
    """
    Generate Basic Authentication header for Lantmäteriet APIs.

    Returns:
        Dict with Authorization header, or None if credentials missing.
    """
    config = lm_config.LANTMATERIET_CONFIG
    if not config.has_credentials():
        logger.warning(
            "Lantmäteriet credentials not configured. "
            "Set LANTMATERIET_USERNAME and LANTMATERIET_PASSWORD in .env file. "
            "Register at https://apimanager.lantmateriet.se/"
        )
        return None

    credentials = f"{config.username}:{config.password}"
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
