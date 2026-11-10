"""
Ground truth for the vanilla resources the generator references.

Every prefab, material and config the generated project points at is a
``{GUID}path`` pair owned by the game. Until v1.18.5 those values were
checked by reading Workbench-saved ``.layer`` files out of public mod
repositories. The game ships something better: ``addons/data/
resourceDatabase.rdb``, the engine's own path -> GUID index.

This module reads that file, collects what ``config/`` references, and keeps
a trimmed snapshot (``fixtures/vanilla_resources.json``) so the comparison
also runs where no game is installed: CI, the Docker image.

    # verify config against an installed game
    python -m tests.vanilla_resources --game-dir "<...>/Arma Reforger"

    # refresh the snapshot after a game update
    python -m tests.vanilla_resources --game-dir "<...>/Arma Reforger" --write

``--game-dir`` defaults to the ``REFORGER_GAME_DIR`` environment variable.

Not runtime code: nothing under ``services/`` imports it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "vanilla_resources.json"
GAME_DIR_ENV = "REFORGER_GAME_DIR"

# Path of the base-game database inside an install, and of the game exe
# whose version resource names the build the snapshot came from.
RDB_RELATIVE_PATH = Path("addons") / "data" / "resourceDatabase.rdb"
GAME_EXE_NAME = "ArmaReforgerSteam.exe"

# Terrain surface materials live in one flat directory. The snapshot keeps
# the whole listing (51 files in 1.9.0.37), not just the ones we map, so a
# future mapping change can be checked without the game installed.
SURFACE_DIR = "Terrains/Common/Surfaces/"

# The Rally DLC is a separate, paid addon. A generated project depends on
# the base game only, so nothing from it may ever be referenced.
RALLY_ADDON_GUID = "6566A2D70498DA6B"
RALLY_PATH_MARKERS = ("RallyLibrary/", "Rally_")

# A parse that silently finds nothing would make every check vacuous, so
# parse_rdb refuses to return unless it sees a plausible database: the real
# one holds ~100,000 files, and this pair has been stable since the terrain
# prefab was first captured (v1.5.0).
_MIN_FILE_ENTRIES = 50_000
_ANCHOR = (
    "Prefabs/World/DefaultWorld/GenericTerrain_Default.et",
    "221ABC927C672E4E",
)

_GUID_PATH = re.compile(r"^\{([0-9A-F]{16})\}(.+)$")

# One database entry, as observed in 1.9 builds (the format is not
# documented by Bohemia):
#
#   u32 LE   length of the path, including its terminating NUL
#   bytes    the path, relative to the addon root, NUL-terminated
#   u8       5 = directory, 6 = file
#   5 bytes  (unused here)
#   u64 LE   the resource GUID, which Enfusion prints everywhere else as
#            16 hex digits, most significant first
#
# The regex finds candidates; the length prefix is what confirms one.
_RDB_ENTRY = re.compile(rb"([\x20-\x7e]{2,400})\x00([\x05\x06]).{5}(.{8})", re.S)
_RDB_KIND_FILE = 6


class ResourceDatabaseError(RuntimeError):
    """The file is not a resource database this parser understands."""


def parse_rdb(rdb_path: Path | str) -> dict[str, str]:
    """Return ``{path: GUID}`` for every file in a ``resourceDatabase.rdb``."""
    blob = Path(rdb_path).read_bytes()
    if blob[:4] != b"FORM" or blob[8:12] != b"RDBC":
        raise ResourceDatabaseError(
            f"{rdb_path}: not an Enfusion resource database "
            f"(header {blob[:12]!r}, expected FORM....RDBC)"
        )

    files: dict[str, str] = {}
    for match in _RDB_ENTRY.finditer(blob):
        start = match.start()
        if start < 4:
            continue
        path = match.group(1)
        (declared_len,) = struct.unpack_from("<I", blob, start - 4)
        if declared_len != len(path) + 1:
            continue
        if match.group(2)[0] != _RDB_KIND_FILE:
            continue
        (guid,) = struct.unpack("<Q", match.group(3))
        files[path.decode("ascii")] = f"{guid:016X}"

    if len(files) < _MIN_FILE_ENTRIES:
        raise ResourceDatabaseError(
            f"{rdb_path}: parsed only {len(files)} file entries, expected at "
            f"least {_MIN_FILE_ENTRIES}. The database format has probably "
            "changed; re-derive the entry layout before trusting any result."
        )
    anchor_path, anchor_guid = _ANCHOR
    if files.get(anchor_path) != anchor_guid:
        raise ResourceDatabaseError(
            f"{rdb_path}: anchor {anchor_path} resolved to "
            f"{files.get(anchor_path)!r}, expected {anchor_guid}. Either the "
            "format changed or Bohemia re-registered the terrain prefab; "
            "both need a human before anything else is believed."
        )
    return files


def read_game_version(game_dir: Path | str) -> str:
    """ProductVersion of the game exe, or ``"unknown"``.

    Read straight out of the PE version resource (a UTF-16 key/value block)
    so the helper needs nothing beyond the standard library.
    """
    exe = Path(game_dir) / GAME_EXE_NAME
    try:
        data = exe.read_bytes()
    except OSError:
        return "unknown"
    key = "ProductVersion".encode("utf-16-le")
    at = data.find(key)
    if at < 0:
        return "unknown"
    tail = data[at + len(key): at + len(key) + 80].decode("utf-16-le", "ignore")
    found = re.search(r"\d+(?:\.\d+){2,3}", tail)
    return found.group(0) if found else "unknown"


def _split(reference: str) -> tuple[str, str]:
    match = _GUID_PATH.match(reference)
    if not match:
        raise ValueError(f"not a {{GUID}}path reference: {reference!r}")
    return match.group(2), match.group(1)


def collect_config_references() -> dict[str, str | None]:
    """Every vanilla resource ``config/`` names, as ``{path: GUID or None}``.

    ``None`` marks a path the generator names without a GUID (surface
    materials, which the user picks in the Resource Browser, and prefabs
    that are catalogued but not emitted). Such a path must still exist.

    Paths come out through the same functions the emitters use, so a
    reference that is built wrongly is caught as well as one that is typed
    wrongly.
    """
    from config import buildings, enfusion, roads

    refs: dict[str, str | None] = {}

    def add(path: str, guid: str | None) -> None:
        known = refs.get(path)
        if known is not None and guid is not None and known != guid:
            raise AssertionError(
                f"config names {path} with two GUIDs: {known} and {guid}"
            )
        refs[path] = guid if guid is not None else known

    for key, path in enfusion.WORLD_PREFABS.items():
        add(path, enfusion.WORLD_PREFAB_GUIDS.get(key))

    for path in set(enfusion.AMBIENT_SOUND_PREFABS.values()):
        add(path, enfusion.AMBIENT_SOUND_PREFAB_GUIDS.get(path))

    for reference in (
        enfusion.WORLD_ENV_SKY_PRESET,
        *enfusion.WORLD_ENV_PLANET_PRESET,
        enfusion.WORLD_ENV_CLOUDS_PRESET,
        enfusion.WORLD_ENV_LENS_FLARES_CONFIG,
    ):
        add(*_split(reference))

    for path in set(buildings.KNOWN_BUILDING_PREFABS.values()):
        resolved = buildings.building_prefab_reference(path)
        add(path, resolved[1] if resolved else None)

    for name in roads.KNOWN_ROAD_PREFABS:
        reference = roads.fully_qualified_road_prefab(name)
        if reference is None:
            raise AssertionError(f"road prefab {name} has no GUID")
        add(*_split(reference))

    for material in surface_material_paths():
        add(material, None)

    return refs


def surface_material_paths() -> list[str]:
    """Addon-relative path of every surface ``.emat`` the generator maps.

    ``SURFACE_MATERIAL_BASE`` is spelled the way the Resource Browser shows
    it, with the addon's display name in front; the database has no such
    prefix.
    """
    from config import enfusion

    names: set[str] = set(enfusion.SURFACE_MATERIAL_MAP.values())
    for alternatives in enfusion.SURFACE_MATERIAL_ALTERNATIVES.values():
        names.update(alternatives)
    prefix = "ArmaReforger/"
    return sorted(
        name[len(prefix):] if name.startswith(prefix) else name for name in names
    )


def load_snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def build_snapshot(game_dir: Path | str) -> dict:
    """Snapshot of an installed game, trimmed to what ``config/`` references."""
    database = parse_rdb(Path(game_dir) / RDB_RELATIVE_PATH)
    references = collect_config_references()
    missing = sorted(path for path in references if path not in database)
    if missing:
        raise ResourceDatabaseError(
            "config references resources this game build does not have; fix "
            "config before writing a snapshot:\n  " + "\n  ".join(missing)
        )
    return {
        "game_version": read_game_version(game_dir),
        "source": RDB_RELATIVE_PATH.as_posix(),
        "resources": {path: database[path] for path in sorted(references)},
        "surface_materials": sorted(
            path[len(SURFACE_DIR):]
            for path in database
            if path.startswith(SURFACE_DIR)
            and path.endswith(".emat")
            and "/" not in path[len(SURFACE_DIR):]
        ),
    }


def compare(references: dict[str, str | None], truth: dict[str, str]) -> list[str]:
    """Human-readable mismatches between ``references`` and ``truth``.

    Paths are compared exactly, case included: Workbench writes the path as
    stored, and a reference that differs only in case is still not what a
    saved layer would contain.
    """
    by_guid = {guid: path for path, guid in truth.items()}
    problems: list[str] = []
    for path, guid in sorted(references.items()):
        actual = truth.get(path)
        if actual is None:
            hint = f"; that GUID belongs to {by_guid[guid]}" if guid in by_guid else ""
            problems.append(f"missing path: {path}{hint}")
        elif guid is not None and actual != guid:
            problems.append(f"wrong GUID: {path} is {actual}, config says {guid}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check config/ against an installed game's resource database."
    )
    parser.add_argument("--game-dir", default=os.environ.get(GAME_DIR_ENV))
    parser.add_argument(
        "--write", action="store_true", help="rewrite the snapshot fixture"
    )
    args = parser.parse_args(argv)
    if not args.game_dir:
        parser.error(f"pass --game-dir or set {GAME_DIR_ENV}")

    database = parse_rdb(Path(args.game_dir) / RDB_RELATIVE_PATH)
    version = read_game_version(args.game_dir)
    references = collect_config_references()
    problems = compare(references, database)
    print(
        f"Arma Reforger {version}: {len(database)} files in the database, "
        f"{len(references)} referenced by config, {len(problems)} problem(s)"
    )
    for problem in problems:
        print("  " + problem)
    if problems:
        return 1
    if args.write:
        snapshot = build_snapshot(args.game_dir)
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"wrote {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
