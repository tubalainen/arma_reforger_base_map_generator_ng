"""
Every vanilla resource the generator names must exist, with the GUID we say.

The failure this guards has happened three times: a path or GUID that looked
right, was never checked against the game, and made Workbench drop the
entity without a word (#111 world prefabs, #151 surface materials, #198
buildings). Each fix was verified by hand against whatever reference was to
hand. This suite checks all of them, every run, against the game's own
resource database.

Two layers:

* **Snapshot** (always on). ``fixtures/vanilla_resources.json`` is a trimmed
  capture of ``resourceDatabase.rdb``. It needs no game install, so it runs
  in CI and in the image.
* **Live** (opt-in). With ``REFORGER_GAME_DIR`` set, the same checks run
  against the installed game, and the snapshot itself is checked for
  staleness. This is the one-command answer to "did the new game version
  change anything for us?".

See ``tests/vanilla_resources.py`` for the database format and the CLI.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from tests import vanilla_resources as vr  # noqa: E402

GAME_DIR = os.environ.get(vr.GAME_DIR_ENV)

requires_game = pytest.mark.skipif(
    not GAME_DIR,
    reason=f"{vr.GAME_DIR_ENV} not set; live resource database check skipped",
)

# `"{GUID}path"` as it appears in a generated layer.
_LAYER_REFERENCE = re.compile(r'"\{([0-9A-F]{16})\}([^"]+)"')


def _metadata():
    return {
        "heightmap": {"dimensions": "2049x2049", "grid_cell_size_m": 2.0},
        "elevation": {
            "min_elevation_m": 0,
            "max_elevation_m": 100,
            "height_scale": 0.03125,
            "height_offset": 0,
        },
        "input": {"bbox": {"south": 0.0, "north": 4.0, "west": 0.0, "east": 4.0}},
    }


def _generator():
    from services.enfusion_project_generator import EnfusionProjectGenerator

    return EnfusionProjectGenerator(map_name="TestMap", metadata=_metadata())


class TestSnapshotIsUsable:
    """Guard the guard: an empty or truncated snapshot would pass everything."""

    def test_snapshot_is_populated(self):
        snapshot = vr.load_snapshot()
        assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", snapshot["game_version"]), (
            "snapshot has no game version; regenerate it with --write"
        )
        assert len(snapshot["resources"]) >= 60
        assert len(snapshot["surface_materials"]) >= 40
        for path, guid in snapshot["resources"].items():
            assert re.fullmatch(r"[0-9A-F]{16}", guid), (path, guid)

    def test_snapshot_holds_the_terrain_anchor(self):
        path, guid = vr._ANCHOR
        assert vr.load_snapshot()["resources"][path] == guid

    def test_config_references_are_collected(self):
        """The collector must actually find the tables it is meant to walk."""
        from config import buildings, enfusion, roads

        references = vr.collect_config_references()
        with_guid = {p for p, g in references.items() if g}
        assert len(with_guid) >= (
            len(enfusion.WORLD_PREFAB_GUIDS)
            + len(buildings.BUILDING_PREFAB_GUIDS)
            + len(roads.PREFAB_GUIDS)
        )
        assert enfusion.WORLD_PREFABS["terrain"] in with_guid
        assert any(p.endswith("/RG_Road_Asphalt_E_01.et") for p in with_guid)
        assert any("/Structures/" in p for p in with_guid)
        assert any(p.endswith("/Grass_01.emat") for p in references)


class TestConfigMatchesSnapshot:
    def test_every_referenced_path_and_guid_matches(self):
        problems = vr.compare(
            vr.collect_config_references(), vr.load_snapshot()["resources"]
        )
        assert not problems, (
            "config/ disagrees with the vanilla resource snapshot "
            f"(Arma Reforger {vr.load_snapshot()['game_version']}):\n  "
            + "\n  ".join(problems)
            + "\nIf the game moved the resource, fix config and refresh the "
            "snapshot with `python -m tests.vanilla_resources --write`."
        )

    def test_every_surface_material_is_a_vanilla_file(self):
        """Failure mode #3 of the contract: a made-up ``.emat`` crashes NVTT
        on the first save after painting (#151, #138)."""
        vanilla = set(vr.load_snapshot()["surface_materials"])
        mapped = [p[len(vr.SURFACE_DIR):] for p in vr.surface_material_paths()]
        assert len(mapped) >= 10
        assert not [name for name in mapped if name not in vanilla]

    def test_no_guid_is_shared_between_two_paths(self):
        resources = vr.load_snapshot()["resources"]
        assert len(set(resources.values())) == len(resources)


class TestGeneratedOutputUsesOnlyVanillaResources:
    """The emitters join a GUID to a path; check the joined result too."""

    def test_layer_references_resolve(self):
        resources = vr.load_snapshot()["resources"]
        gen = _generator()
        found = 0
        for layer in (gen._generate_default_layer(), gen._generate_managers_layer()):
            for guid, path in _LAYER_REFERENCE.findall(layer):
                found += 1
                assert resources.get(path) == guid, (
                    f'layer emits "{{{guid}}}{path}", but the game has '
                    f"{resources.get(path)!r} at that path"
                )
        # world + terrain + lighting + fog + PP + probe + 7 env + 9 managers
        # + ambient. Fewer means the regex or the emitters changed shape.
        assert found >= 20

    def test_project_depends_on_the_base_game_only(self):
        """1.9 added the Rally DLC as a second official addon. A generated
        project must load for everyone, so it depends on the base game and
        nothing else."""
        from config.enfusion import ARMA_REFORGER_GUID

        gproj = _generator()._generate_gproj()
        block = re.search(r"Dependencies\s*\{([^}]*)\}", gproj)
        assert block, gproj
        assert re.findall(r'"([0-9A-F]{16})"', block.group(1)) == [ARMA_REFORGER_GUID]
        assert vr.RALLY_ADDON_GUID not in gproj

    def test_nothing_references_the_rally_addon(self):
        for path, guid in vr.collect_config_references().items():
            assert guid != vr.RALLY_ADDON_GUID, path
            assert not any(marker in path for marker in vr.RALLY_PATH_MARKERS), path


class TestSetupGuideQuotesOnlyVanillaResources:
    """SETUP_GUIDE.md is what the user actually reads and pastes from. Until
    v1.18.5 it told them road generators were at one path while printing
    another in the examples, and neither was current."""

    @pytest.fixture(scope="class")
    def guide(self, tmp_path_factory):
        from services.setup_guide_generator import SetupGuideGenerator
        from tests.test_setup_guide_generator import _metadata as guide_metadata

        meta = guide_metadata(
            surfaces_present=["grass", "asphalt"],
            coverage_per_surface={
                "grass": {"percentage": 75.0},
                "asphalt": {"percentage": 25.0},
            },
            road_count=12,
        )
        path = SetupGuideGenerator("TestMap", meta).generate(
            tmp_path_factory.mktemp("guide")
        )
        return path.read_text(encoding="utf-8")

    def test_every_guid_reference_in_the_guide_resolves(self, guide):
        from config.enfusion import ARMA_REFORGER_GUID

        resources = vr.load_snapshot()["resources"]
        references = [
            (guid, path)
            for guid, path in re.findall(
                r"\{([0-9A-F]{16})\}([\w/.\-]+\.(?:et|emat|conf))", guide
            )
            # The addon GUID only appears in the guide inside quoted error
            # messages, as the example of what *not* to write.
            if guid != ARMA_REFORGER_GUID
        ]
        assert len(references) >= 3, "the guide no longer prints fq: examples"
        for guid, path in references:
            assert resources.get(path) == guid, (
                f"SETUP_GUIDE prints {{{guid}}}{path}, which is not what the "
                f"game has at that path ({resources.get(path)!r})"
            )

    def test_guide_names_the_current_road_generator_directory(self, guide):
        from config.enfusion import ROAD_PREFAB_BASE

        assert ROAD_PREFAB_BASE in guide
        assert "}PrefabLibrary/Generators/Roads/" not in guide


@requires_game
class TestAgainstInstalledGame:
    """Run with REFORGER_GAME_DIR set, after every game update."""

    @pytest.fixture(scope="class")
    def database(self):
        return vr.parse_rdb(Path(GAME_DIR) / vr.RDB_RELATIVE_PATH)

    def test_config_matches_the_installed_game(self, database):
        problems = vr.compare(vr.collect_config_references(), database)
        assert not problems, (
            f"config/ disagrees with Arma Reforger {vr.read_game_version(GAME_DIR)}"
            ":\n  " + "\n  ".join(problems)
        )

    def test_snapshot_is_current(self, database):
        fresh = vr.build_snapshot(GAME_DIR)
        stored = vr.load_snapshot()
        assert stored == fresh, (
            f"the snapshot was captured from {stored['game_version']} and the "
            f"installed game is {fresh['game_version']}; review the diff, then "
            "refresh with `python -m tests.vanilla_resources --write`."
        )
