"""Convert a downloaded PBF extract into the bzip2 XML the importer demands.

Overpass's `init_osm3s.sh` runs `bunzip2 <$PLANET_FILE | update_database`, so
the import needs bzip2-compressed OSM XML — and no mirror publishes that for
country extracts, only `.osm.pbf`. Something has to convert.

Doing it with `osmium cat -o file.osm.bz2` costs an hour or more for a country
the size of Sweden, all of it on **one core**, because libosmium compresses the
output stream serially. On a twenty-core server that is nineteen cores watching.

So the conversion runs here, in the init container, where we control the image
and can install a parallel compressor:

    osmium cat -f osm -o - extract.pbf | pbzip2 -c -p N > extract.osm.bz2

pbzip2 emits a concatenation of standard bzip2 streams, which plain `bunzip2`
decompresses without knowing the difference. osmium's XML *generation* stays
serial but is far cheaper than the compression it was feeding, so the speedup
tracks the thread count closely.

The second benefit is bigger than the first: the converted file lands on the
extract volume, so a failed import no longer reconverts. Before this, retrying
an import meant paying the whole conversion again.

If neither parallel compressor is present — an older image, a hand-built one —
`available()` reports so and the caller leaves the conversion to the sidecar,
exactly as it worked before.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# bzip2's file magic. Enough to tell a real archive from a truncated pipe.
BZIP2_MAGIC = b"BZh"

# Preferred first. Both produce output that standard bunzip2 accepts; pbzip2 is
# the more widely packaged, lbzip2 usually the faster.
PARALLEL_COMPRESSORS = ("pbzip2", "lbzip2")


@dataclass
class ConversionOutcome:
    ok: bool
    reason: str
    path: Optional[Path] = None
    already_present: bool = False
    threads: int = 0


def default_threads() -> int:
    """How many compressor threads to use.

    Half the machine by default. The init container often shares a box with
    whatever else the operator runs, and a conversion that pegs every core for
    ten minutes is its own kind of rude. `OVERPASS_CONVERT_THREADS` overrides.
    """
    raw = os.getenv("OVERPASS_CONVERT_THREADS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning(f"Ignoring unparseable OVERPASS_CONVERT_THREADS={raw!r}")
    return max(1, (os.cpu_count() or 2) // 2)


def compressor() -> Optional[str]:
    """First parallel bzip2 implementation on PATH, or None."""
    for name in PARALLEL_COMPRESSORS:
        if shutil.which(name):
            return name
    return None


def available() -> bool:
    """Can this container do the conversion itself?"""
    return bool(shutil.which("osmium")) and bool(compressor())


def looks_like_bzip2(path: Path) -> bool:
    try:
        if path.stat().st_size < len(BZIP2_MAGIC) + 1:
            return False
        with path.open("rb") as fh:
            return fh.read(len(BZIP2_MAGIC)) == BZIP2_MAGIC
    except OSError:
        return False


def convert(
    pbf: Path, destination: Path, threads: Optional[int] = None
) -> ConversionOutcome:
    """Convert `pbf` to bzip2-compressed OSM XML at `destination`.

    Writes through a `.part` file so an interrupted conversion never leaves
    something that looks finished — the import would otherwise be handed a
    truncated archive and fail an hour later for the wrong reason.
    """
    if looks_like_bzip2(destination):
        return ConversionOutcome(
            ok=True,
            reason="Already converted; reusing it.",
            path=destination,
            already_present=True,
        )

    if not available():
        return ConversionOutcome(
            ok=False,
            reason=(
                "No parallel bzip2 (pbzip2/lbzip2) or osmium in this image — "
                "leaving the conversion to the sidecar, single-threaded."
            ),
        )

    if not pbf.is_file():
        return ConversionOutcome(ok=False, reason=f"No extract to convert at {pbf}.")

    n = threads or default_threads()
    tool = compressor()
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")

    # osmium writes XML to stdout; the compressor is the only expensive half,
    # so it gets the threads. Both stderr streams are captured and only shown
    # when something fails — osmium is chatty about perfectly fine input.
    reader = ["osmium", "cat", "-f", "osm", "-o", "-", str(pbf)]
    writer = [tool, "-c", "-p" + str(n)] if tool == "pbzip2" else [tool, "-c", "-n", str(n)]

    try:
        with part.open("wb") as out:
            first = subprocess.Popen(reader, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert first.stdout is not None
            second = subprocess.Popen(
                writer, stdin=first.stdout, stdout=out, stderr=subprocess.PIPE
            )
            # Let osmium see a broken pipe if the compressor dies.
            first.stdout.close()
            _, writer_err = second.communicate()
            _, reader_err = first.communicate()
    except OSError as e:
        part.unlink(missing_ok=True)
        return ConversionOutcome(ok=False, reason=f"Could not run the conversion: {e}")

    if first.returncode != 0 or second.returncode != 0:
        part.unlink(missing_ok=True)
        detail = (reader_err or b"").decode(errors="replace").strip()
        detail = detail or (writer_err or b"").decode(errors="replace").strip()
        return ConversionOutcome(
            ok=False,
            reason=(
                f"Conversion failed (osmium exit {first.returncode}, "
                f"{tool} exit {second.returncode}): {detail[:500]}"
            ),
        )

    if not looks_like_bzip2(part):
        part.unlink(missing_ok=True)
        return ConversionOutcome(
            ok=False, reason="Conversion produced something that is not a bzip2 file."
        )

    part.replace(destination)
    size = destination.stat().st_size
    return ConversionOutcome(
        ok=True,
        reason=f"Converted to {size / 1e9:.2f} GB of bzip2 XML using {n} {tool} threads.",
        path=destination,
        threads=n,
    )
