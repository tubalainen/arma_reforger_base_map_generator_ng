"""
Tests for EnfusionProjectGenerator — GUID uniqueness (issue #61).

Before the fix, world.ent.meta and mission.conf.meta both used project_guid,
causing a "duplicate GUID" registration error in Workbench.
"""

from __future__ import annotations

import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


class _IdentityTransformer:
    def transform_points(self, points, elevation_array=None):
        return [
            {"x": round(p["x"] * 1000, 3), "y": 0.0, "z": round(p["y"] * 1000, 3)}
            for p in points
        ]


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


def _make_gen(**kwargs):
    from services.enfusion_project_generator import EnfusionProjectGenerator
    return EnfusionProjectGenerator(
        map_name="TestMap",
        metadata=_metadata(),
        transformer=_IdentityTransformer(),
        elevation_array=None,
        **kwargs,
    )


class TestGuidUniqueness:
    def test_three_guids_are_all_different(self):
        gen = _make_gen()
        assert gen.project_guid != gen.world_ent_guid, (
            "project_guid and world_ent_guid must differ"
        )
        assert gen.project_guid != gen.mission_conf_guid, (
            "project_guid and mission_conf_guid must differ"
        )
        assert gen.world_ent_guid != gen.mission_conf_guid, (
            "world_ent_guid and mission_conf_guid must differ — "
            "duplicate GUID causes Workbench registration errors (issue #61)"
        )

    def test_world_ent_meta_uses_world_ent_guid(self):
        gen = _make_gen()
        meta = gen._generate_meta("ent", "Worlds/TestMap.ent", gen.world_ent_guid)
        assert gen.world_ent_guid in meta
        assert gen.project_guid not in meta, (
            "world.ent.meta must use world_ent_guid, not project_guid"
        )

    def test_mission_conf_meta_uses_mission_conf_guid(self):
        gen = _make_gen()
        meta = gen._generate_meta("conf", "Missions/TestMap.conf", gen.mission_conf_guid)
        assert gen.mission_conf_guid in meta
        assert gen.project_guid not in meta, (
            "mission.conf.meta must use mission_conf_guid, not project_guid"
        )

    def test_mission_conf_world_reference_uses_world_ent_guid(self):
        gen = _make_gen()
        conf = gen._generate_mission_conf()
        assert gen.world_ent_guid in conf, (
            "mission.conf World field must reference world_ent_guid so Workbench "
            "can resolve the world file"
        )
        assert gen.project_guid not in conf, (
            "mission.conf must not use project_guid as a resource reference"
        )

    def test_guids_are_deterministic(self):
        gen1 = _make_gen()
        gen2 = _make_gen()
        assert gen1.project_guid == gen2.project_guid
        assert gen1.world_ent_guid == gen2.world_ent_guid
        assert gen1.mission_conf_guid == gen2.mission_conf_guid

    def test_guids_differ_across_map_names(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator
        gen_a = EnfusionProjectGenerator(
            map_name="MapA", metadata=_metadata(),
            transformer=_IdentityTransformer(), elevation_array=None,
        )
        gen_b = EnfusionProjectGenerator(
            map_name="MapB", metadata=_metadata(),
            transformer=_IdentityTransformer(), elevation_array=None,
        )
        assert gen_a.world_ent_guid != gen_b.world_ent_guid
        assert gen_a.mission_conf_guid != gen_b.mission_conf_guid
