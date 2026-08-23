"""Optional self-hosted Overpass sidecar.

A local Overpass instance removes the pipeline's last unreliable dependency:
no rate limits, no 504s, no volunteer-run mirror going down mid-generation.
It is entirely optional — with `OVERPASS_LOCAL_COUNTRIES` unset the app
behaves exactly as it does without a sidecar.

The instance holds a **country extract, not the planet**, so it is wired into
the pool through the same country gate as any other regional mirror (see
`OVERPASS_REGIONAL_ENDPOINTS` in endpoints.py). Outside its coverage the
public planet mirrors take over automatically.

Geofabrik publishes a daily diff stream per extract, which the sidecar applies
itself; nothing here needs a cron job.
"""

import os

# Country code -> (Geofabrik extract path, PBF size in GB).
#
# Every path and its `-updates/` diff directory was verified live against
# download.geofabrik.de on 2026-08-23. Sizes are the published PBF; the
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

# Geofabrik publishes .osm.pbf, but the Overpass importer runs
# `bunzip2 < planet.osm.bz2 | update_database` and requires bzip2-compressed
# OSM XML — no .osm.bz2 exists for any country extract. The sidecar's
# entrypoint eval's OVERPASS_PLANET_PREPROCESS between downloading the file and
# importing it, so the conversion happens there.
#
# This default is baked into the launcher the init step writes, which means it
# ships in *this image* rather than in docker-compose.yml — operators who only
# run `docker compose pull` get the fix without hand-editing their compose file.
# A value supplied through the environment still wins; see write_launcher().
DEFAULT_PLANET_PREPROCESS = (
    "mv -f /db/planet.osm.bz2 /db/planet.osm.pbf && "
    "osmium cat --overwrite -o /db/planet.osm.bz2 /db/planet.osm.pbf && "
    "rm -f /db/planet.osm.pbf"
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


# How stale the sidecar's data may get before the UI flags it. Geofabrik
# publishes country diffs once a day, so two missed days means the update
# loop is stuck rather than merely between runs.
LOCAL_STALE_AFTER_HOURS = int(_env("OVERPASS_LOCAL_STALE_AFTER_HOURS", "48") or "48")

# The sidecar is on a private Docker network with no other tenants, so it
# needs no politeness cap — but keep it bounded so one job can't saturate it.
LOCAL_SLOTS = int(_env("OVERPASS_LOCAL_SLOTS", "8") or "8")
