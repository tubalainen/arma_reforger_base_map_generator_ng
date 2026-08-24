"""Optional self-hosted Overpass sidecar.

A local Overpass instance removes the pipeline's last unreliable dependency:
no rate limits, no 504s, no volunteer-run mirror going down mid-generation.
It is entirely optional — with `OVERPASS_LOCAL_COUNTRIES` unset the app
behaves exactly as it does without a sidecar.

The instance holds a **country extract, not the planet**, so it is wired into
the pool through the same country gate as any other regional mirror (see
`OVERPASS_REGIONAL_ENDPOINTS` in endpoints.py). Outside its coverage the
public planet mirrors take over automatically.

Each mirror publishes a diff stream per extract, which the sidecar applies
itself; nothing here needs a cron job.

Two mirrors are known: Geofabrik (the default, daily diffs) and OpenStreetMap
France (minutely diffs, fewer countries). Geofabrik firewalls IP addresses that
re-download large extracts repeatedly, and a failing import used to do exactly
that on every restart — so an operator who gets blocked needs a way out that
does not involve editing docker-compose.yml. Hence `OVERPASS_LOCAL_MIRROR`,
plus raw `OVERPASS_PLANET_URL` / `OVERPASS_DIFF_URL` overrides for a private
mirror or a hand-downloaded `file:///` extract.
"""

import os

# Country code -> (Geofabrik extract path, PBF size in GB).
#
# Every path and its `-updates/` diff directory was verified live against
# download.geofabrik.de on 2026-08-23. Sizes are Geofabrik's published PBF and
# are used only for log lines and disk estimates — other mirrors cut their
# extracts differently and run larger (OSM France's Sweden is 0.90 GB against
# Geofabrik's 0.76), so treat the figure as a floor, not a measurement. The
# Overpass database that gets built from it runs roughly 8-12x larger, so
# budget accordingly — Sweden's 0.76 GB extract becomes ~8 GB on disk.
#
# Covers every country in COUNTRY_NAMES. Note the paths that don't follow
# "europe/<lowercased name>": GB, CZ, IE and RU.
GEOFABRIK_EXTRACTS: dict[str, tuple[str, float]] = {
    "SE": ("europe/sweden", 0.76),
    "NO": ("europe/norway", 1.28),
    "DK": ("europe/denmark", 0.46),
    "FI": ("europe/finland", 0.69),
    "EE": ("europe/estonia", 0.11),
    "LV": ("europe/latvia", 0.13),
    "LT": ("europe/lithuania", 0.21),
    "DE": ("europe/germany", 4.49),
    "PL": ("europe/poland", 1.94),
    "RU": ("russia", 3.86),
    "GB": ("europe/great-britain", 2.01),
    "FR": ("europe/france", 4.72),
    "ES": ("europe/spain", 1.37),
    "IT": ("europe/italy", 2.07),
    "AT": ("europe/austria", 0.75),
    "CH": ("europe/switzerland", 0.51),
    "CZ": ("europe/czech-republic", 0.88),
    "NL": ("europe/netherlands", 1.30),
    "BE": ("europe/belgium", 0.64),
    "UA": ("europe/ukraine", 0.81),
    "RO": ("europe/romania", 0.30),
    "HU": ("europe/hungary", 0.30),
    "SK": ("europe/slovakia", 0.32),
    "HR": ("europe/croatia", 0.19),
    "RS": ("europe/serbia", 0.22),
    "BG": ("europe/bulgaria", 0.16),
    "GR": ("europe/greece", 0.32),
    "PT": ("europe/portugal", 0.39),
    "IE": ("europe/ireland-and-northern-ireland", 0.38),
    "IS": ("europe/iceland", 0.06),
}

# Whole-continent extracts, for operators who want more than one country.
# Only reachable by setting OVERPASS_LOCAL_REGION explicitly — resolving a
# multi-country list to one of these automatically would mean silently
# downloading 32 GB because someone typed "SE,NO".
PARENT_EXTRACTS: dict[str, float] = {
    "europe": 32.45,
}

# ---------------------------------------------------------------------------
# Mirrors
# ---------------------------------------------------------------------------
# Geofabrik is the default and the only one with full coverage of the table
# above. OpenStreetMap France is the escape hatch for operators Geofabrik has
# firewalled: same data, different slugs, minutely diffs, ~20 countries.
#
# Every OSMFR path below was verified live against download.openstreetmap.fr
# on 2026-08-24 — both the `-latest.osm.pbf` and the `minute/state.txt`.
GEOFABRIK_BASE = "https://download.geofabrik.de"
OSMFR_BASE = "https://download.openstreetmap.fr"

DEFAULT_MIRROR = "geofabrik"

# Geofabrik region path -> OSM France region path. The Geofabrik path stays the
# canonical identity of an extract (it is what the region marker records and
# what the UI shows), so switching mirrors never looks like a region change and
# never triggers a re-import.
#
# Absent entries are countries OSM France does not publish: EE, LV, LT, RO, HU,
# HR, RS, BG, GR, IS. Selecting osmfr for one of those is a configuration
# error, not a silent fallback — see mirror_region().
#
# Note GB: OSM France has no great-britain extract, only united_kingdom, which
# additionally contains Northern Ireland. A superset is harmless for the
# country gate, and the bbox clips it anyway.
OSMFR_REGIONS: dict[str, str] = {
    "europe/sweden": "europe/sweden",
    "europe/norway": "europe/norway",
    "europe/denmark": "europe/denmark",
    "europe/finland": "europe/finland",
    "europe/germany": "europe/germany",
    "europe/poland": "europe/poland",
    "russia": "russia",
    "europe/great-britain": "europe/united_kingdom",
    "europe/france": "europe/france",
    "europe/spain": "europe/spain",
    "europe/italy": "europe/italy",
    "europe/austria": "europe/austria",
    "europe/switzerland": "europe/switzerland",
    "europe/czech-republic": "europe/czech_republic",
    "europe/netherlands": "europe/netherlands",
    "europe/belgium": "europe/belgium",
    "europe/ukraine": "europe/ukraine",
    "europe/slovakia": "europe/slovakia",
    "europe/portugal": "europe/portugal",
    "europe/ireland-and-northern-ireland": "europe/ireland",
    "europe": "europe",
}

MIRRORS = ("geofabrik", "osmfr")

# Seconds between diff sweeps. A local OSM copy that is a few days behind is
# indistinguishable from a fresh one for terrain generation — roads and
# coastlines do not move weekly — so sweeping once a week is plenty, and it is
# an order of magnitude gentler on the mirror than the daily default. Whatever
# accumulated (7 daily diffs from Geofabrik, ~10k tiny minutely ones from OSM
# France) is applied in a single catch-up pass.
DEFAULT_UPDATE_SLEEP_SECONDS = 7 * 24 * 3600

# Floor for an operator-supplied OVERPASS_UPDATE_SLEEP. Both mirrors are run by
# volunteers and neither owes us a diff every minute; a typo of "60" here would
# mean 1440 requests a day for data that changes once.
MIN_UPDATE_SLEEP_SECONDS = 3600

# Grace period on top of one sweep before the UI calls the data stale. Anything
# shorter and a correctly-working weekly sweep would spend most of the week
# reporting a problem.
STALE_GRACE_HOURS = 48

# ---------------------------------------------------------------------------
# Download preparation
# ---------------------------------------------------------------------------
# Both mirrors publish .osm.pbf, but the Overpass importer runs
# `bunzip2 < planet.osm.bz2 | update_database` and requires bzip2-compressed
# OSM XML — no .osm.bz2 exists for any country extract. The sidecar's
# entrypoint eval's OVERPASS_PLANET_PREPROCESS between downloading the file and
# importing it, so the conversion happens there.
#
# From v1.13.0 the conversion normally happens in the init container instead,
# where a parallel bzip2 is installed — see services/overpass_extract_converter.
# What runs here is then only a guard. Which of the two scripts gets written is
# decided at init time; both ship in *this image* rather than in
# docker-compose.yml, so operators who only run `docker compose pull` get fixes
# without hand-editing their compose file. A value supplied through the
# environment still wins over either.

# Where the pristine downloaded PBF is kept between import attempts, inside the
# database volume so it dies with the database on a region change. Skipped by
# _db_is_populated() in the init script — a cached extract is not a database.
CACHE_DIR_NAME = "extract_cache"


def cache_filename(region: str) -> str:
    """Downloaded-extract file name for a canonical (Geofabrik) region path."""
    return "planet-" + region.replace("/", "_") + ".osm.pbf"


def converted_filename(region: str) -> str:
    """Name of the bzip2-XML conversion of that extract.

    Kept beside the PBF so a failed import reuses it. Converting is the single
    most expensive step in bringing a sidecar up, and before v1.13.0 every
    retry paid for it again.
    """
    return "planet-" + region.replace("/", "_") + ".osm.bz2"


DEFAULT_PLANET_PREPROCESS = "sh /overpass_meta/preprocess.sh"

# Two preprocessing scripts, both eval'd by the sidecar's entrypoint between
# staging the planet file and importing it.
#
# GUARD_ONLY is the normal path from v1.13.0: the init step already converted
# the extract, so all that is left is to confirm the file survived the copy.
# The upstream entrypoint stages it with curl and treats exit code 000 as
# success (that is the `file://` code), so a truncated or missing file reads as
# success and the import fails an hour later complaining about the wrong thing.
#
# CONVERT_AND_GUARD is the fallback for an image without osmium-tool and a
# parallel bzip2 — it does the conversion in the sidecar, single-threaded, the
# way every version before v1.13.0 did.
_GUARD = """if [ ! -s "$SRC" ]; then
    echo "FATAL: the extract handed to the importer is empty." >&2
    echo "  Source was $OVERPASS_PLANET_URL. curl reports 000 for both a" >&2
    echo "  file:// read and a connection that never opened, so the" >&2
    echo "  entrypoint cannot tell an empty copy from a successful one." >&2
    echo "  Check the overpass-local-init logs: it prepares the extract and" >&2
    echo "  refuses to start this container without one." >&2
    exit 1
fi
"""

GUARD_ONLY_SCRIPT = (
    """#!/bin/sh
# Written by overpass_local_init.py. Do not edit in the container: the file
# lives on a read-only mount and is regenerated on every `docker compose up`.
set -e

SRC=/db/planet.osm.bz2

"""
    + _GUARD
    + """
if ! head -c 3 "$SRC" | grep -qa BZh; then
    echo "FATAL: the file handed to the importer is not a bzip2 archive." >&2
    echo "  Got $(wc -c < "$SRC") bytes from $OVERPASS_PLANET_URL starting with:" >&2
    head -c 300 "$SRC" >&2
    echo >&2
    exit 1
fi

echo "Extract already converted by the init step; importing directly."
"""
)

CONVERT_AND_GUARD_SCRIPT = (
    """#!/bin/sh
# Written by overpass_local_init.py. Do not edit in the container: the file
# lives on a read-only mount and is regenerated on every `docker compose up`.
set -e

SRC=/db/planet.osm.bz2

"""
    + _GUARD
    + """
if ! head -c 64 "$SRC" | grep -qa OSMHeader; then
    echo "FATAL: the extract handed to the importer is not an OSM PBF file." >&2
    echo "  Got $(wc -c < "$SRC") bytes from $OVERPASS_PLANET_URL starting with:" >&2
    head -c 300 "$SRC" >&2
    echo >&2
    exit 1
fi

echo "Converting the PBF to bzip2 XML for the importer (single-threaded --" >&2
echo "this image has no parallel bzip2; expect an hour or more)..." >&2
mv -f "$SRC" /db/planet.osm.pbf
osmium cat --overwrite -o "$SRC" /db/planet.osm.pbf
rm -f /db/planet.osm.pbf
"""
)

# Kept for compatibility with anything referring to the old single name.
PREPROCESS_SCRIPT = CONVERT_AND_GUARD_SCRIPT

# ---------------------------------------------------------------------------
# Download budget
# ---------------------------------------------------------------------------
# A user was firewalled by Geofabrik after their sidecar pulled 300+ GB: the
# v1.10.0 container had `restart: unless-stopped`, a failing import left no
# database, and every restart re-downloaded the whole extract. Roughly 400
# iterations of a 0.76 GB file.
#
# The structural fix is that the Overpass container no longer downloads
# anything — our init step fetches the extract once, to a volume of its own,
# and hands the sidecar a `file://` URL. These limits are the backstop for when
# that fetch itself keeps failing. They live on the extract volume, so they
# survive container recreation, `docker compose up` in a loop, and a
# `docker volume rm ..._overpass_db` — the three things that defeat any counter
# held in a container.
EXTRACT_MOUNT = "/extract"
LEDGER_NAME = "download_ledger.json"

# Attempts allowed inside one cooldown window. Three is enough to ride out a
# flaky connection and few enough that a genuinely broken config stops early.
MAX_DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_COOLDOWN_HOURS = 6

# Bytes allowed from a mirror per window, as a multiple of the extract size.
# Downloads resume from where they stopped, so needing more than three times
# the file means something is pathologically wrong — a server ignoring Range,
# a proxy truncating — and that is exactly the case worth stopping.
DOWNLOAD_BUDGET_MULTIPLIER = 3.0
DOWNLOAD_BUDGET_WINDOW_HOURS = 24

DOWNLOAD_CHUNK_BYTES = 8 * 1024 * 1024
# Generous: a 32 GB continent extract on a slow line. Resume covers the rest.
DOWNLOAD_READ_TIMEOUT_S = 120.0
DOWNLOAD_CONNECT_TIMEOUT_S = 30.0

# A lock so two init containers cannot fetch the same extract at once.
# Abandoned locks (a killed container) expire rather than wedging the stack.
DOWNLOAD_LOCK_STALE_HOURS = 12


def download_user_agent() -> str:
    """Identify the app to the mirror.

    An operator whose deployment misbehaves is much better off being emailed
    than silently firewalled, and that only happens if the requests say who
    they are.
    """
    from config.enfusion import APP_VERSION

    return (
        f"ArmaReforgerBaseMapGenerator/{APP_VERSION} "
        f"(+https://github.com/tubalainen/arma_reforger_base_map_generator_ng)"
    )


DEFAULT_LOCAL_URL = "http://overpass-local/api/interpreter"

# Where the sidecar's init step records which extract it built. Shared with
# the app through a small named volume so a config change that hasn't been
# applied to the running container yet is visible in the UI.
DEFAULT_MARKER_PATH = "/overpass_meta/region.txt"


def _env_marker_path() -> str:
    return _env("OVERPASS_LOCAL_MARKER_PATH", DEFAULT_MARKER_PATH)


class LocalOverpassConfigError(ValueError):
    """The sidecar configuration cannot be resolved to a single extract."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def local_countries() -> list[str]:
    """Country codes the sidecar's extract is declared to cover."""
    raw = _env("OVERPASS_LOCAL_COUNTRIES")
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def local_region() -> str:
    """Geofabrik extract path for the sidecar, or "" when it is disabled.

    `OVERPASS_LOCAL_REGION` wins when set, which is the escape hatch for
    continent-sized or custom extracts. Otherwise the region is derived from
    a single country code.

    Raises:
        LocalOverpassConfigError: more than one country was listed without an
            explicit region, or the country has no known extract. Both are
            configuration mistakes that would otherwise surface as a silently
            wrong or enormous download.
    """
    explicit = _env("OVERPASS_LOCAL_REGION")
    if explicit:
        return explicit

    countries = local_countries()
    if not countries:
        return ""

    if len(countries) > 1:
        raise LocalOverpassConfigError(
            f"OVERPASS_LOCAL_COUNTRIES lists {len(countries)} countries "
            f"({', '.join(countries)}), but one Overpass sidecar holds one "
            f"extract. Either list a single country, or set "
            f"OVERPASS_LOCAL_REGION=europe explicitly "
            f"(~{PARENT_EXTRACTS['europe']} GB compressed, 300 GB+ on disk)."
        )

    country = countries[0]
    if country not in GEOFABRIK_EXTRACTS:
        raise LocalOverpassConfigError(
            f"No Geofabrik extract is mapped for country '{country}'. "
            f"Known: {', '.join(sorted(GEOFABRIK_EXTRACTS))}."
        )
    return GEOFABRIK_EXTRACTS[country][0]


def local_extract_size_gb() -> float:
    """Published PBF size for the configured extract, 0.0 if unknown."""
    region = local_region()
    if not region:
        return 0.0
    if region in PARENT_EXTRACTS:
        return PARENT_EXTRACTS[region]
    for path, size in GEOFABRIK_EXTRACTS.values():
        if path == region:
            return size
    return 0.0


def mirror() -> str:
    """Which download mirror the sidecar pulls its extract from.

    Raises:
        LocalOverpassConfigError: an unknown mirror name. Falling back to the
            default here would quietly send a blocked operator straight back
            to the mirror that blocked them.
    """
    name = _env("OVERPASS_LOCAL_MIRROR", DEFAULT_MIRROR).lower() or DEFAULT_MIRROR
    if name not in MIRRORS:
        raise LocalOverpassConfigError(
            f"Unknown OVERPASS_LOCAL_MIRROR '{name}'. "
            f"Known: {', '.join(MIRRORS)}."
        )
    return name


def mirror_region() -> str:
    """The configured region as the selected mirror spells it.

    `local_region()` stays canonical (Geofabrik spelling) because it is the
    extract's identity; this is the same extract translated into whatever the
    chosen mirror calls it.

    An explicit `OVERPASS_LOCAL_REGION` is passed through untranslated — it is
    the escape hatch for custom extracts, so the operator owns the spelling.

    Raises:
        LocalOverpassConfigError: the mirror does not publish this region.
    """
    region = local_region()
    if not region:
        return ""

    name = mirror()
    if name == "geofabrik" or _env("OVERPASS_LOCAL_REGION"):
        return region

    translated = OSMFR_REGIONS.get(region)
    if not translated:
        raise LocalOverpassConfigError(
            f"OpenStreetMap France does not publish an extract for "
            f"'{region}'. Either use OVERPASS_LOCAL_MIRROR=geofabrik, or set "
            f"OVERPASS_PLANET_URL and OVERPASS_DIFF_URL to a mirror that does."
        )
    return translated


def planet_url() -> str:
    """URL the sidecar downloads its extract from, "" when disabled.

    `OVERPASS_PLANET_URL` wins outright, which is how an operator points at a
    private mirror or at a `file:///` path holding a hand-downloaded extract.
    """
    explicit = _env("OVERPASS_PLANET_URL")
    if explicit:
        return explicit

    region = mirror_region()
    if not region:
        return ""
    if mirror() == "osmfr":
        return f"{OSMFR_BASE}/extracts/{region}-latest.osm.pbf"
    return f"{GEOFABRIK_BASE}/{region}-latest.osm.pbf"


def diff_url() -> str:
    """Replication directory the sidecar applies diffs from, "" when disabled.

    Both mirrors use the standard osmosis layout (`state.txt` plus
    `NNN/NNN/NNN.osc.gz`), so they are interchangeable to the importer — they
    differ only in cadence, which update_sleep_seconds() accounts for.
    """
    explicit = _env("OVERPASS_DIFF_URL")
    if explicit:
        return explicit

    region = mirror_region()
    if not region:
        return ""
    if mirror() == "osmfr":
        return f"{OSMFR_BASE}/replication/{region}/minute/"
    return f"{GEOFABRIK_BASE}/{region}-updates/"


def update_sleep_seconds() -> int:
    """Seconds the sidecar waits between diff sweeps.

    Clamped from below: the mirrors are volunteer-run and a too-eager value
    here turns one deployment into a steady stream of requests against them.
    """
    raw = _env("OVERPASS_UPDATE_SLEEP")
    try:
        requested = int(raw) if raw else DEFAULT_UPDATE_SLEEP_SECONDS
    except ValueError:
        requested = DEFAULT_UPDATE_SLEEP_SECONDS
    return max(requested, MIN_UPDATE_SLEEP_SECONDS)


def local_stale_after_hours() -> int:
    """How old the sidecar's data may get before the UI flags it.

    Derived from the sweep interval rather than fixed, so raising the interval
    cannot leave the UI permanently complaining about a healthy sidecar. An
    explicit OVERPASS_LOCAL_STALE_AFTER_HOURS still wins.
    """
    explicit = _env("OVERPASS_LOCAL_STALE_AFTER_HOURS")
    if explicit:
        try:
            return int(explicit)
        except ValueError:
            pass
    return update_sleep_seconds() // 3600 + STALE_GRACE_HOURS


def local_url() -> str:
    """Interpreter URL of the sidecar."""
    return _env("OVERPASS_LOCAL_URL", DEFAULT_LOCAL_URL)


def local_enabled() -> bool:
    """Is a sidecar configured at all?"""
    try:
        return bool(local_region()) and bool(local_countries())
    except LocalOverpassConfigError:
        # A broken config is still "enabled" — the operator meant to turn it
        # on, and the error belongs in the status report, not silently off.
        return True


def local_only() -> bool:
    """Refuse public mirrors entirely (private / air-gapped deployments)."""
    return _env("OVERPASS_LOCAL_ONLY").lower() in ("1", "true", "yes")



# The sidecar is on a private Docker network with no other tenants, so it
# needs no politeness cap — but keep it bounded so one job can't saturate it.
LOCAL_SLOTS = int(_env("OVERPASS_LOCAL_SLOTS", "8") or "8")
