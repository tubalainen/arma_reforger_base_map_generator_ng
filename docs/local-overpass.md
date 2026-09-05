# Self-Hosted OSM Data (Local Overpass)

By default the app fetches OpenStreetMap features from a pool of public Overpass
mirrors. Those are volunteer-run, rate-limited, and occasionally down. If that
becomes a problem you can run your own Overpass instance alongside the app,
holding a single country's data.

This is **entirely optional**. Leave `OVERPASS_LOCAL_COUNTRIES` unset and nothing
changes.

## Enable it

Pick one country by ISO code in `.env`:

```bash
OVERPASS_LOCAL_COUNTRIES=SE
```

Then start the extra services with the `local-osm` profile:

```bash
docker compose --profile local-osm up -d
```

Supported codes: `SE NO DK FI EE LV LT DE PL RU GB FR ES IT AT CH CZ NL BE UA RO
HU SK HR RS BG GR PT IE IS`

> **Upgrading from v1.9.0 or earlier?** The sidecar adds two services and two
> volumes to `docker-compose.yml`, and adds one volume mount to the existing
> `arma-map-generator` service. Pull the latest `docker-compose.yml` from this
> repository, or see [Upgrading an existing deployment](setup.md#upgrading-an-existing-deployment).

## What happens on first boot

The sidecar downloads that country's [Geofabrik](https://download.geofabrik.de/)
extract (or another mirror's — see below) and builds an Overpass database. This takes **hours** for a large country
and needs roughly **10x the compressed extract on disk** — Sweden's 0.76 GB
extract lands near 8 GB.

There is an extra step you will see in the logs before the import starts: the
mirrors publish `.osm.pbf`, but the Overpass importer requires bzip2-compressed
OSM XML, so the file is converted first. The init container does this with a
parallel compressor across half your cores — a few minutes on a multi-core box,
where doing it inside the Overpass container took an hour or more on a single
thread. Set `OVERPASS_CONVERT_THREADS` to change how many it uses.

The converted archive is kept alongside the extract, so retrying a failed import
costs neither a download nor a reconversion.

The extract is downloaded once, by the init step, onto a volume of its own.
The Overpass container is handed a local file and is never given a mirror URL,
so no restart of it can cost bandwidth. Budget the compressed size once more on
top of the figure above; it is deleted automatically once the database is built.

Because the download happens in the init step, the first
`docker compose --profile local-osm up -d` **blocks while the extract
downloads** — progress is in `docker compose logs overpass-local-init`, and an
interrupted transfer resumes rather than starting over.

If the download keeps failing, it stops rather than retrying forever: three
failed attempts in six hours, or three times the extract size pulled in
twenty-four, and the init step refuses and exits non-zero, so the sidecar never
starts. That is deliberate — repeatedly re-downloading an extract is what gets
an IP firewalled. The ledger lives on the extract volume, so it survives
`--force-recreate`; clear it with `docker volume rm arma-map-generator_overpass_extract`
once the underlying problem is fixed.

Nothing breaks meanwhile. The app keeps using public mirrors, and a banner in the
sidebar shows the sidecar's progress. Watch it with:

```bash
docker compose logs -f overpass-local
```

## Staying up to date

The sidecar applies the mirror's diffs itself — there is no cron job to set up
and nothing to run manually. It sweeps **once a week** by default: OSM road and
coastline geometry does not move fast enough for a terrain generator to care,
and both mirrors are volunteer-run. Set `OVERPASS_UPDATE_SLEEP` lower if you
want fresher data; values under an hour are clamped.

## If the mirror stops answering

Geofabrik firewalls IP addresses that re-download large extracts repeatedly, and
the block is a silent packet drop — connections time out rather than returning
an HTTP error, from a browser as well as from Docker. If that happens, switch
mirrors without changing anything else:

```bash
OVERPASS_LOCAL_MIRROR=osmfr
```

[download.openstreetmap.fr](https://download.openstreetmap.fr/) serves the same
data with minutely diffs. Treat it as the fallback, not the destination:
Geofabrik covers every country here and publishes one diff a day, where OSM
France cuts fewer countries, runs slightly larger extracts (Sweden 0.90 GB
against 0.76), and publishes only minutely replication — about 1,400 small
diff fetches a day whatever `OVERPASS_UPDATE_SLEEP` is set to, since every
sequence gets fetched eventually. Move back to Geofabrik once you can. It covers SE NO DK FI DE PL RU GB FR ES IT AT CH CZ NL
BE UA SK PT IE — but **not** EE, LV, LT, RO, HU, HR, RS, BG, GR or IS; choosing
it for one of those is reported as a configuration error rather than failing
mid-import. The region marker is mirror-independent, so switching does **not**
trigger a re-import.

For anything else — a private mirror, or an extract you downloaded by hand and
dropped on the volume — set the URLs directly:

```bash
OVERPASS_PLANET_URL=file:///db/extract_cache/planet-europe_sweden.osm.pbf
OVERPASS_DIFF_URL=https://download.openstreetmap.fr/replication/europe/sweden/minute/
```

Both override `OVERPASS_LOCAL_MIRROR` entirely. The diff directory must use the
standard osmosis layout (`state.txt` plus `NNN/NNN/NNN.osc.gz`).

## Changing country

Edit `OVERPASS_LOCAL_COUNTRIES` in `.env`, then recreate the sidecar:

```bash
docker compose --profile local-osm up -d --force-recreate overpass-local
```

It notices the change, clears the old database and re-imports. Until you do this,
the web UI flags that the running container no longer matches your `.env`.

## How it gets used

You do not choose per generation. The sidecar holds one country, so the app uses
it automatically for areas inside that country and the public mirror pool
everywhere else — there is no way to accidentally ask a Sweden database about
France. Which source served a generation is recorded in the Activity Log and in
the SETUP_GUIDE's Data Sources appendix.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `OVERPASS_LOCAL_COUNTRIES` | *(unset)* | One ISO country code. Unset disables the sidecar entirely. |
| `OVERPASS_LOCAL_REGION` | *(derived)* | Override with a raw extract path, e.g. `europe`, for more than one country. Much larger. |
| `OVERPASS_LOCAL_MIRROR` | `geofabrik` | Where the extract comes from: `geofabrik` (all countries, daily diffs) or `osmfr` (fewer countries, minutely diffs). |
| `OVERPASS_PLANET_URL` | *(derived)* | Full escape hatch — a private mirror, or `file:///…` for a hand-downloaded extract. Overrides the mirror. |
| `OVERPASS_DIFF_URL` | *(derived)* | Replication directory, osmosis layout. Empty imports once and never updates. |
| `OVERPASS_UPDATE_SLEEP` | `604800` | Seconds between diff sweeps. Clamped to a minimum of 3600 — the mirrors are volunteer-run. |
| `OVERPASS_LOCAL_ONLY` | `0` | `1` refuses public mirrors entirely. Generations fail rather than fall back — for private or air-gapped deployments. |
| `OVERPASS_LOCAL_STALE_AFTER_HOURS` | *(derived)* | Flag the sidecar as stale after this long without a diff. Defaults to one sweep plus 48 h. |

## Checking status

```bash
curl -s localhost:8080/api/osm/local-status
```

| State | Meaning |
|---|---|
| `disabled` | No sidecar configured — the normal setup |
| `importing` | Configured, not answering yet; public mirrors in use |
| `ready` | Serving, data fresh |
| `stale` | Serving, but no diff applied recently — check the logs |
| `restart_required` | `.env` asks for a different country than the running container built |
| `misconfigured` | Configuration cannot resolve to a single extract |

---

[← Back to the README](../README.md)
