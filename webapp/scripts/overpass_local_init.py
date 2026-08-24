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
    EXTRACT_MOUNT,
    LEDGER_NAME,
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
from services.overpass_extract_fetcher import fetch_extract  # noqa: E402

DB_DIR = Path("/db")
META_DIR = Path("/overpass_meta")
EXTRACT_DIR = Path(EXTRACT_MOUNT)


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


def extract_dir() -> Path:
    """Where the downloaded extract lives.

    Its own volume when docker-compose.yml mounts one, which is the point: the
    documented recovery step is `docker volume rm ..._overpass_db`, and that
    must not throw away a gigabyte that has already been fetched.

    Falls back into the database volume for compose files predating the mount,
    with a warning — the fallback still works, it just loses the extract on a
    database wipe.
    """
    if EXTRACT_DIR.is_mount():
        return EXTRACT_DIR
    log(
        f"WARNING: no volume mounted at {EXTRACT_MOUNT}, keeping the extract "
        f"inside the database volume instead. Add `overpass_extract:{EXTRACT_MOUNT}` "
        f"to docker-compose.yml — otherwise wiping the database volume also "
        f"discards the downloaded extract and costs another full download."
    )
    return DB_DIR / CACHE_DIR_NAME


def _prune_stale_extracts(region: str, keep_dir: Path) -> None:
    """Drop extracts this run cannot use.

    Two cases: one for a different region, and one whose import has since
    finished. Keeping either only burns a gigabyte of volume — and re-fetching
    is never the cheap option, so anything still needed is left alone.
    """
    if not keep_dir.is_dir():
        return

    keep = "" if _import_succeeded() else cache_filename(region)
    for child in keep_dir.iterdir():
        if child.name in (keep, LEDGER_NAME, ".download.lock"):
            continue
        if child.name == keep + ".part":
            continue
        try:
            child.unlink()
            log(f"Dropped {child.name} (no longer needed).")
        except OSError as e:
            log(f"WARNING: could not remove {child}: {e}")


def _log_progress(done: int, total: int) -> None:
    """Report roughly every 5%, or every 500 MB when the size is unknown."""
    if total:
        pct = done * 100 // total
        if pct >= _log_progress.last + 5:
            _log_progress.last = pct
            log(f"  ... {pct}% ({done / 1e9:.2f} / {total / 1e9:.2f} GB)")
    else:
        step = done // (500 * 1024 * 1024)
        if step > _log_progress.last:
            _log_progress.last = step
            log(f"  ... {done / 1e9:.2f} GB")


_log_progress.last = 0


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

    cadence = "minutely" if mirror_name == "osmfr" else "daily"
    approx = "~" if mirror_name == "geofabrik" else ">"
    log(f"Region:    {region}  (countries: {', '.join(countries) or 'n/a'})")
    log(f"Mirror:    {mirror_name}")
    log(f"Extract:   {planet}  ({approx}{size_gb} GB compressed)")
    log(f"Diffs:     {diffs}  ({cadence}, swept every {update_sleep_seconds()}s)")
    if not _db_is_populated():
        log(
            f"NOTE: first import of a {size_gb} GB extract (Geofabrik's figure; "
            f"other mirrors run larger). The mirrors ship PBF and the Overpass "
            f"importer requires bzip2 XML, so the file is converted before the "
            f"import — that conversion alone can take an hour or more. Budget "
            f"roughly {size_gb * 10 + size_gb:.0f} GB of disk; the extract is "
            f"kept until the import succeeds so a retry costs no download. The "
            f"web app keeps using public mirrors throughout."
        )

    extracts = extract_dir()
    _prune_stale_extracts(region, extracts)

    (META_DIR / "planet_url.txt").write_text(planet, encoding="utf-8")
    (META_DIR / "diff_url.txt").write_text(diffs, encoding="utf-8")
    marker.write_text(region, encoding="utf-8")

    # The guard-and-convert step the entrypoint eval's before importing.
    (META_DIR / "preprocess.sh").write_text(PREPROCESS_SCRIPT, encoding="utf-8")

    extract_file = extracts / cache_filename(region)

    # Fetch the extract here rather than letting the Overpass container do it.
    # Its entrypoint re-downloads whenever there is no database, which turned a
    # failing import into 300+ GB of traffic and a firewall block. Downloading
    # once, under a budget, on a volume that survives a database wipe, is the
    # whole point of this step.
    if _import_succeeded():
        log("The database is already built — no extract needed.")
    elif planet.startswith("file://"):
        # An operator pointed OVERPASS_PLANET_URL at their own file. Nothing to
        # fetch, and nothing of ours should copy bytes it did not download.
        log(f"Using the operator-supplied extract at {planet}.")
        extract_file = None
    else:
        log(f"Fetching the extract from {planet}")
        outcome = fetch_extract(
            planet, extract_file, expected_gb=size_gb, on_progress=_log_progress
        )
        log(f"  {outcome.reason}")
        if not outcome.ok:
            log(
                "ERROR: no extract available, so the sidecar will not be "
                "started. Nothing is retried automatically — see the message "
                "above and TROUBLESHOOTING.md."
            )
            return 1
        if outcome.already_present:
            log("  (no bytes pulled from the mirror)")

    planet_for_sidecar = (
        f"file://{extract_file.as_posix()}" if extract_file else planet
    )

    # Launcher for the sidecar. Writing the resolved URL into a script keeps
    # every "$" out of docker-compose.yml, where escaping them is easy to get
    # subtly wrong and hard to test without running the stack.
    #
    # It is always a local path: the Overpass container has no reason to talk
    # to a download mirror, and not giving it one means no restart of it can
    # ever cost a download.
    launcher = [
        "#!/bin/sh",
        "set -e",
        f"export OVERPASS_DIFF_URL='{diffs}'",
        f"export OVERPASS_PLANET_URL='{planet_for_sidecar}'",
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

    log(f"Launcher:  {(META_DIR / 'start.sh').as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
