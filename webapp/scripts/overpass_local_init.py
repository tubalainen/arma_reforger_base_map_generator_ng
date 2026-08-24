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
import stat
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, "/app")

from config.overpass_local import (  # noqa: E402
    CACHE_DIR_NAME,
    CONVERT_AND_GUARD_SCRIPT,
    DEFAULT_PLANET_PREPROCESS,
    EXTRACT_MOUNT,
    GUARD_ONLY_SCRIPT,
    LEDGER_NAME,
    LocalOverpassConfigError,
    cache_filename,
    converted_filename,
    diff_url,
    local_countries,
    local_extract_size_gb,
    local_region,
    mirror,
    planet_url,
    update_sleep_seconds,
)
from services import overpass_extract_converter as converter  # noqa: E402
from services import overpass_replication as replication  # noqa: E402
from services.overpass_extract_fetcher import (  # noqa: E402
    fetch_extract,
    last_successful_download,
)

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

    keep = set()
    if not _import_succeeded():
        keep = {cache_filename(region), converted_filename(region)}
        keep |= {name + ".part" for name in keep}
    protected = keep | {LEDGER_NAME, ".download.lock"}

    for child in keep_dir.iterdir():
        if child.name in protected:
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


# How far before the extract's download time to resume when the extract itself
# is gone and its replication timestamp with it. Geofabrik cuts its extracts up
# to a day before publishing them, so two days of margin covers the gap.
# Re-applying changes the database already has is harmless; missing some is not.
RECOVERY_MARGIN_HOURS = 48


def _ensure_db_traversable() -> None:
    """Make /db traversable by the Overpass image's non-owner services.

    The image creates /db as the `overpass` user's home directory, and Debian
    bookworm's adduser defaults home directories to 0700. The next line in that
    Dockerfile fixes ownership but not the mode. Meanwhile supervisord runs
    fcgiwrap as user `nginx`, which then cannot traverse into /db to reach the
    dispatcher socket — every query fails with

        runtime error: open64: 13 Permission denied /db/db//osm3s_osm_base

    while the database itself is perfectly fine. A named volume copies the mode
    from the image, so the fault is baked in at first start and survives every
    restart. We run as root here and can simply correct it.
    """
    try:
        mode = stat.S_IMODE(DB_DIR.stat().st_mode)
    except OSError as e:
        log(f"WARNING: cannot inspect {DB_DIR}: {e}")
        return

    if mode & 0o055 == 0o055:
        return

    try:
        DB_DIR.chmod(mode | 0o055)
        log(
            f"Made {DB_DIR} traversable (was {mode:04o}) — the Overpass image "
            f"runs its query CGI as a different user than it owns /db with."
        )
    except OSError as e:
        log(f"WARNING: could not adjust permissions on {DB_DIR}: {e}")


def _seed_replication(diffs: str, extract_file, extracts: Path) -> None:
    """Give the sidecar's update loop a starting point.

    Without /db/replicate_id the loop cannot start and cannot bootstrap itself,
    so the sidecar serves data that silently ages forever. The upstream image
    seeds this during import through a script whose exit status is swallowed by
    a pipe, so a momentary network fault loses it permanently — which is
    exactly what happened to the deployment this was written for.
    """
    sequence_file = DB_DIR / "replicate_id"

    target = None
    source = ""
    if extract_file is not None and extract_file.is_file():
        target = replication.replication_timestamp_from_pbf(extract_file)
        source = "the extract's replication header"
    if target is None:
        downloaded = last_successful_download(extracts)
        if downloaded is not None:
            target = downloaded - timedelta(hours=RECOVERY_MARGIN_HOURS)
            source = (
                f"the download time less {RECOVERY_MARGIN_HOURS}h "
                f"(the extract is gone, so its header is too)"
            )

    outcome = replication.seed(sequence_file, diffs, target)
    if outcome.already_present:
        return
    if outcome.ok and outcome.sequence is not None:
        log(f"Replication: {outcome.reason} Derived from {source}.")
    elif outcome.ok:
        log(f"Replication: {outcome.reason}")
    else:
        log(
            f"WARNING: {outcome.reason} The sidecar will serve queries but its "
            f"data will not update. Re-run this container once the mirror is "
            f"reachable."
        )


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

    _ensure_db_traversable()

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

    extract_file = extracts / cache_filename(region)
    converted_file = extracts / converted_filename(region)

    # Fetch the extract here rather than letting the Overpass container do it.
    # Its entrypoint re-downloads whenever there is no database, which turned a
    # failing import into 300+ GB of traffic and a firewall block. Downloading
    # once, under a budget, on a volume that survives a database wipe, is the
    # whole point of this step.
    if _import_succeeded():
        log("The database is already built — no extract needed.")
    elif converter.looks_like_bzip2(converted_file):
        # A previous run already converted this extract, and the PBF was
        # dropped once it had. Re-fetching it would be a pure waste of the
        # mirror's bandwidth — the archive is what the import consumes.
        log("The converted extract is already on the volume; nothing to do.")
        extract_file = None
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

    # Before converting, not after: the replication headers this needs live in
    # the PBF and do not survive the conversion, and the PBF is dropped once
    # converted.
    _seed_replication(diffs, extract_file, extracts)

    # Convert here rather than in the sidecar when this image can: the Overpass
    # image ships bzip2 but no parallel implementation, so `osmium cat -o
    # x.osm.bz2` there pins one core for an hour on a country-sized extract
    # while the rest of the machine idles. Converting once, here, also means a
    # failed import no longer pays for the conversion again.
    convert_in_sidecar = True
    if converter.looks_like_bzip2(converted_file):
        convert_in_sidecar = False
    elif extract_file is None or _import_succeeded():
        pass
    elif not converter.available():
        log(
            "NOTE: no parallel bzip2 in this image, so the sidecar will convert "
            "the extract itself, single-threaded. Expect an hour or more."
        )
    else:
        log(
            f"Converting the extract to bzip2 XML with "
            f"{converter.default_threads()} threads "
            f"(set OVERPASS_CONVERT_THREADS to change)."
        )
        result = converter.convert(extract_file, converted_file)
        log(f"  {result.reason}")
        if result.ok:
            convert_in_sidecar = False
            # The PBF has served its purpose. The archive is what a retry would
            # reuse now, so it is the one worth the disk.
            if extract_file.is_file():
                extract_file.unlink(missing_ok=True)
                log("  Dropped the PBF; the converted archive supersedes it.")
        else:
            log("  Falling back to converting inside the sidecar.")

    if not convert_in_sidecar:
        planet_for_sidecar = f"file://{converted_file.as_posix()}"
    elif extract_file is None:
        planet_for_sidecar = planet
    else:
        planet_for_sidecar = f"file://{extract_file.as_posix()}"

    (META_DIR / "preprocess.sh").write_text(
        CONVERT_AND_GUARD_SCRIPT if convert_in_sidecar else GUARD_ONLY_SCRIPT,
        encoding="utf-8",
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
