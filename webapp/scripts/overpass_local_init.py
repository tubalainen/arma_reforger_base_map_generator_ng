#!/usr/bin/env python3
"""Prepare the Overpass sidecar's volumes before the instance starts.

Runs as a one-shot init container ahead of `overpass-local`. It exists to
solve one problem: the Overpass image decides whether to import by looking for
an existing database, so changing `OVERPASS_LOCAL_COUNTRIES` in `.env` would
otherwise be silently ignored and the sidecar would keep serving the old
country forever.

This script:

1. resolves the configured country code to an extract on the configured
   mirror, using the same mapping the app uses — one source of truth, so the
   container and the app can never disagree about what is loaded;
2. compares it against the marker written by the previous run;
3. wipes the database volume when they differ, so the next start re-imports;
4. publishes the resolved URLs for the sidecar's command to pick up, and the
   region marker for the web UI to display;
5. writes the preprocessing script that guards and caches the download.

Re-import happens in the background as far as the app is concerned: the web
app keeps serving from public mirrors while the sidecar rebuilds, and only
starts using it again once it answers queries.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from config.overpass_local import (  # noqa: E402
    CACHE_DIR_NAME,
    DEFAULT_PLANET_PREPROCESS,
    PREPROCESS_SCRIPT,
    LocalOverpassConfigError,
    cache_filename,
    diff_url,
    local_countries,
    local_extract_size_gb,
    local_region,
    mirror,
    planet_url,
    update_sleep_seconds,
)

DB_DIR = Path("/db")
META_DIR = Path("/overpass_meta")


def log(message: str) -> None:
    print(f"[overpass-local-init] {message}", flush=True)


def _db_is_populated() -> bool:
    """Has a previous run actually built a database here?

    An empty or absent directory means a first run, where there is nothing to
    wipe and nothing to compare against. A cached extract does not count — it
    lives in the same volume but it is an input, not a database.
    """
    if not DB_DIR.is_dir():
        return False
    return any(child.name != CACHE_DIR_NAME for child in DB_DIR.iterdir())


def _import_succeeded() -> bool:
    """Did the last import finish? The entrypoint's own marker says so."""
    return (DB_DIR / "init_done").is_file()


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


def _prune_stale_caches(region: str) -> None:
    """Drop cached extracts that this run cannot use.

    Two cases: a cache for a different region (left over from a config change
    that did not trigger a wipe), and a cache whose import has since finished —
    keeping either only burns a gigabyte of volume.
    """
    cache_dir = DB_DIR / CACHE_DIR_NAME
    if not cache_dir.is_dir():
        return

    keep = "" if _import_succeeded() else cache_filename(region)
    for child in cache_dir.iterdir():
        if child.name == keep:
            continue
        try:
            child.unlink()
            log(f"Dropped cached extract {child.name} (no longer needed).")
        except OSError as e:
            log(f"WARNING: could not remove {child}: {e}")


def main() -> int:
    try:
        region = local_region()
        mirror_name = mirror()
        planet = planet_url()
        diffs = diff_url()
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

    _prune_stale_caches(region)

    (META_DIR / "planet_url.txt").write_text(planet, encoding="utf-8")
    (META_DIR / "diff_url.txt").write_text(diffs, encoding="utf-8")
    marker.write_text(region, encoding="utf-8")

    # The guard-cache-convert step the entrypoint eval's after downloading.
    (META_DIR / "preprocess.sh").write_text(PREPROCESS_SCRIPT, encoding="utf-8")

    cache_file = DB_DIR / CACHE_DIR_NAME / cache_filename(region)
    cache_path = cache_file.as_posix()

    # Launcher for the sidecar. Writing the resolved URLs into a script keeps
    # every "$" out of docker-compose.yml, where escaping them is easy to get
    # subtly wrong and hard to test without running the stack.
    #
    # The cache check has to live here rather than in this script, because the
    # init container runs once per `docker compose up` while the sidecar may
    # restart several times under `restart: on-failure` — and it is precisely
    # those restarts that must not re-download the extract.
    launcher = [
        "#!/bin/sh",
        "set -e",
        f"export OVERPASS_DIFF_URL='{diffs}'",
        f"export OVERPASS_EXTRACT_CACHE='{cache_path}'",
        'if [ -s "$OVERPASS_EXTRACT_CACHE" ]; then',
        '    echo "Reusing the cached extract; skipping the download."',
        '    export OVERPASS_PLANET_URL="file://$OVERPASS_EXTRACT_CACHE"',
        "else",
        f"    export OVERPASS_PLANET_URL='{planet}'",
        "fi",
        # ":=" assigns only when unset or empty, so an OVERPASS_PLANET_PREPROCESS
        # or OVERPASS_UPDATE_SLEEP passed in through docker-compose still wins
        # over these defaults.
        f': "${{OVERPASS_PLANET_PREPROCESS:={DEFAULT_PLANET_PREPROCESS}}}"',
        "export OVERPASS_PLANET_PREPROCESS",
        f': "${{OVERPASS_UPDATE_SLEEP:={update_sleep_seconds()}}}"',
        "export OVERPASS_UPDATE_SLEEP",
        "exec /app/docker-entrypoint.sh",
        "",
    ]
    (META_DIR / "start.sh").write_text("\n".join(launcher), encoding="utf-8")

    cadence = "minutely" if mirror_name == "osmfr" else "daily"
    log(f"Region:    {region}  (countries: {', '.join(countries) or 'n/a'})")
    log(f"Mirror:    {mirror_name}")
    approx = "~" if mirror_name == "geofabrik" else ">"
    log(f"Extract:   {planet}  ({approx}{size_gb} GB compressed)")
    log(f"Diffs:     {diffs}  ({cadence}, swept every {update_sleep_seconds()}s)")
    if cache_file.is_file():
        log(f"Cache:     {cache_path} — the sidecar will import from disk.")
    if not _db_is_populated():
        log(
            f"NOTE: first import of a {size_gb} GB extract (Geofabrik's "
            f"figure; other mirrors run larger). The mirrors ship PBF "
            f"and the Overpass importer requires bzip2 XML, so the file is "
            f"converted after download — that conversion alone can take an hour "
            f"or more before the import even starts. Budget roughly "
            f"{size_gb * 10 + size_gb:.0f} GB of disk (the extract is cached "
            f"until the import succeeds). The web app keeps using public "
            f"mirrors throughout."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
