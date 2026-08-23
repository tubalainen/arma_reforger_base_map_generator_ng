"""Health and freshness reporting for the optional Overpass sidecar.

The sidecar imports a multi-gigabyte extract on first boot and re-imports
whenever its configured region changes, which takes long enough that the web
UI has to be able to say "it's still building" rather than just failing over
in silence. This module answers three questions:

* Is the sidecar reachable and serving queries yet?
* Does the extract it holds match what the configuration now asks for?
* Has its daily diff loop stalled?

Nothing here is on the generation hot path — a broken or absent sidecar
degrades to the public mirror pool, which is the normal configuration.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from config.overpass_local import (
    LOCAL_SLOTS,
    _env_marker_path,
    LOCAL_STALE_AFTER_HOURS,
    LocalOverpassConfigError,
    local_countries,
    local_enabled,
    local_only,
    local_region,
    local_extract_size_gb,
    local_url,
)

logger = logging.getLogger(__name__)

# The sidecar is a container away on a private network, so it should answer in
# single-digit milliseconds. A long wait means it is still importing, not that
# it is slow — and this runs once per generation, so it must not linger.
_STATUS_TIMEOUT = 3.0

_PROBE_QUERY = "[out:json][timeout:10];node(1);out ids;"


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.rstrip("Z")).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


async def get_local_status() -> dict:
    """Describe the sidecar for the UI and the Activity Log.

    Returns a dict that is always safe to serialise, with `state` one of:

    * `disabled`    — no sidecar configured; the normal setup
    * `misconfigured` — configuration cannot resolve to one extract
    * `importing`   — configured but not answering queries yet
    * `ready`       — serving, data fresh
    * `stale`       — serving, but the diff loop looks stuck
    * `restart_required` — serving an extract the config no longer asks for
    """
    if not local_enabled():
        return {"state": "disabled", "enabled": False}

    try:
        region = local_region()
        countries = local_countries()
    except LocalOverpassConfigError as e:
        return {
            "state": "misconfigured",
            "enabled": True,
            "message": str(e),
        }

    status = {
        "state": "importing",
        "enabled": True,
        "region": region,
        "countries": countries,
        "extract_size_gb": local_extract_size_gb(),
        "local_only": local_only(),
        "url": local_url(),
    }

    try:
        async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT) as client:
            resp = await client.post(
                local_url(),
                data={"data": _PROBE_QUERY},
                headers={"User-Agent": "ArmaReforgerMapGenerator/1.0"},
            )
        if resp.status_code != 200:
            status["message"] = (
                f"Sidecar answered HTTP {resp.status_code} — still importing "
                f"the {region} extract, or the import failed."
            )
            return status
        payload = resp.json()
    except Exception as e:
        # Unreachable is the expected state during the initial import, which
        # can run for hours — so this is informational, not an error.
        status["message"] = (
            f"Sidecar not answering yet ({type(e).__name__}) — importing the "
            f"{region} extract. Public mirrors are being used meanwhile."
        )
        return status

    timestamp = payload.get("osm3s", {}).get("timestamp_osm_base", "")
    data_time = _parse_timestamp(timestamp)
    status["data_timestamp"] = timestamp

    # The sidecar reports which extract it actually built, so a config change
    # that has not been applied yet is visible rather than silently ignored.
    served_region = _read_served_region()
    if served_region:
        status["served_region"] = served_region
        if served_region != region:
            status["state"] = "restart_required"
            status["message"] = (
                f"Sidecar is serving the '{served_region}' extract but the "
                f"configuration now asks for '{region}'. Restart the sidecar "
                f"to re-import: docker compose --profile local-osm up -d "
                f"--force-recreate overpass-local"
            )
            return status

    if data_time is not None:
        age_hours = (datetime.now(timezone.utc) - data_time).total_seconds() / 3600
        status["data_age_hours"] = round(age_hours, 1)
        if age_hours > LOCAL_STALE_AFTER_HOURS:
            status["state"] = "stale"
            status["message"] = (
                f"Sidecar data is {age_hours / 24:.1f} days old. Geofabrik "
                f"publishes daily diffs, so the update loop may be stuck — "
                f"check `docker compose logs overpass-local`."
            )
            return status

    status["state"] = "ready"
    status["slots"] = LOCAL_SLOTS
    status["message"] = f"Local Overpass ready ({region}, data {timestamp})."
    return status


def _read_served_region() -> Optional[str]:
    """Read the region marker the sidecar's init step wrote.

    Shared through a small named volume rather than the sidecar's HTTP root,
    because the Overpass image serves `/api/*` through FastCGI and does not
    serve static files. Absent marker is treated as "unknown" rather than
    "mismatched", so it can never block a generation.
    """
    try:
        path = Path(_env_marker_path())
        if path.is_file():
            return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


async def local_endpoint_if_ready() -> Optional[dict]:
    """The sidecar as a pool endpoint, or None if it isn't usable right now.

    Called once per generation. Returning None simply leaves the public pool
    untouched, which is what makes the whole feature optional.
    """
    if not local_enabled():
        return None

    status = await get_local_status()
    if status["state"] not in ("ready", "stale"):
        if status["state"] != "disabled":
            logger.info(
                f"Local Overpass not in use: {status.get('message', status['state'])}"
            )
        return None

    return {
        "url": local_url(),
        "label": "Local Overpass",
        "slots": LOCAL_SLOTS,
        "local": True,
    }
