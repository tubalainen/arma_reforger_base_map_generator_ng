#!/usr/bin/env python3
"""Prepare the Overpass sidecar's volumes before the instance starts.

Runs as a one-shot init container ahead of `overpass-local`. It exists to
solve one problem: the Overpass image decides whether to import by looking for
an existing database, so changing `OVERPASS_LOCAL_COUNTRIES` in `.env` would
otherwise be silently ignored and the sidecar would keep serving the old
country forever.

This script:

1. resolves the configured country code to a Geofabrik extract, using the
   same mapping the app uses — one source of truth, so the container and the
   app can never disagree about what is loaded;
2. compares it against the marker written by the previous run;
3. wipes the database volume when they differ, so the next start re-imports;
4. publishes the resolved URLs for the sidecar's command to pick up, and the
   region marker for the web UI to display.

Re-import happens in the background as far as the app is concerned: the web
app keeps serving from public mirrors while the sidecar rebuilds, and only
starts using it again once it answers queries.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from config.overpass_local import (  # noqa: E402
    LocalOverpassConfigError,
    local_countries,
    local_extract_size_gb,
    local_region,
)

DB_DIR = Path("/db")
META_DIR = Path("/overpass_meta")
GEOFABRIK = "https://download.geofabrik.de"


def log(message: str) -> None:
    print(f"[overpass-local-init] {message}", flush=True)


def _db_is_populated() -> bool:
    """Has a previous run actually built a database here?

    An empty or absent directory means a first run, where there is nothing to
    wipe and nothing to compare against.
    """
    if not DB_DIR.is_dir():
        return False
    return any(DB_DIR.iterdir())


def _wipe_db() -> None:
    """Clear the database volume so the Overpass image re-imports on start."""
    for child in DB_DIR.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as e:
            log(f"WARNING: could not remove {child}: {e}")


def main() -> int:
    try:
        region = local_region()
    except LocalOverpassConfigError as e:
        log(f"ERROR: {e}")
        return 1

    if not region:
        log(
            "No region resolved — set OVERPASS_LOCAL_COUNTRIES (e.g. SE) or "
            "OVERPASS_LOCAL_REGION. Nothing to do."
        )
        return 1

    countries = local_countries()
    size_gb = local_extract_size_gb()
    planet_url = f"{GEOFABRIK}/{region}-latest.osm.pbf"
    diff_url = f"{GEOFABRIK}/{region}-updates/"

    META_DIR.mkdir(parents=True, exist_ok=True)
    marker = META_DIR / "region.txt"
    previous = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""

    if previous and previous != region and _db_is_populated():
        log(
            f"Configured region changed: '{previous}' -> '{region}'. "
            f"Clearing the database volume so the sidecar re-imports."
        )
        _wipe_db()
        # Drop the marker until the new import is under way, so the UI reports
        # "importing" rather than claiming the new region is already served.
        marker.unlink(missing_ok=True)
    elif previous == region:
        log(f"Region unchanged ('{region}') — keeping the existing database.")
    else:
        log(f"First run for region '{region}' — the sidecar will import it.")

    (META_DIR / "planet_url.txt").write_text(planet_url, encoding="utf-8")
    (META_DIR / "diff_url.txt").write_text(diff_url, encoding="utf-8")
    marker.write_text(region, encoding="utf-8")

    # Launcher for the sidecar. Writing the resolved URLs into a script keeps
    # every "$" out of docker-compose.yml, where escaping them is easy to get
    # subtly wrong and hard to test without running the stack.
    launcher = [
        "#!/bin/sh",
        "set -e",
        f"export OVERPASS_PLANET_URL='{planet_url}'",
        f"export OVERPASS_DIFF_URL='{diff_url}'",
        "exec /app/docker-entrypoint.sh",
        "",
    ]
    (META_DIR / "start.sh").write_text("\n".join(launcher), encoding="utf-8")

    log(f"Region:    {region}  (countries: {', '.join(countries) or 'n/a'})")
    log(f"Extract:   {planet_url}  (~{size_gb} GB compressed)")
    log(f"Diffs:     {diff_url}  (Geofabrik publishes these daily)")
    if not _db_is_populated():
        log(
            f"NOTE: the initial import of a {size_gb} GB extract takes a long "
            f"time and needs roughly {size_gb * 10:.0f} GB of disk. The web app "
            f"keeps using public mirrors until it finishes."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
