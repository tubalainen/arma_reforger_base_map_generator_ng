"""
Tests for v1.4.0 Atlas 2 alignment:

- Atlas 2 canonical road prefab catalogue in config.roads
- Bootstrap-entity completeness in the managers layer
- Biome-matched AmbientSounds_*.et selection
- surface_assignments tracking on the generator

Source of truth for canonical names: "The Atlas 2: Arma Reforger Terrain
Creation Guide" (Jakerod).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


# ---------------------------------------------------------------------------
# Road prefab catalogue
# ---------------------------------------------------------------------------

class TestAtlas2RoadCatalog:
    """All Atlas 2 canonical road prefab names must be present, and every
    derived mapping must point at one of them."""

    def test_known_road_prefabs_contains_atlas2_names(self):
        from config.roads import KNOWN_ROAD_PREFABS
        atlas2 = {
            "RG_Road_Asphalt_E_01",
            "RG_Road_Asphalt_E_01_DashedLine",
            "RG_Road_Asphalt_E_01_Narrow",
            "RG_Road_Asphalt_E_02",
            "RG_Road_Asphalt_E_03",
            "RG_Road_Cobblestone_01",
            "RG_Road_Dirt_01",
            "RG_Road_Dirt_02",
            "RG_Road_Forest_01",
            "RG_TrailDirt_01",
            "RG_TrailGravel_01",
        }
        missing = atlas2 - KNOWN_ROAD_PREFABS
        assert not missing, f"Atlas 2 prefab(s) missing from catalogue: {missing}"

    def test_every_osm_tag_maps_to_known_prefab(self):
        from config.roads import OSM_ROAD_TAGS, KNOWN_ROAD_PREFABS
        for tag, info in OSM_ROAD_TAGS.items():
            assert info["enfusion_prefab"] in KNOWN_ROAD_PREFABS, (
                f"Highway type {tag!r} → {info['enfusion_prefab']!r} "
                f"is not in KNOWN_ROAD_PREFABS"
            )

    def test_every_width_class_maps_to_known_prefab(self):
        from config.roads import ROAD_PREFAB_BY_CLASS, KNOWN_ROAD_PREFABS
        for key, prefab in ROAD_PREFAB_BY_CLASS.items():
            assert prefab in KNOWN_ROAD_PREFABS, (
                f"Width-class {key} → {prefab!r} not in catalogue"
            )

    def test_legacy_8m_name_is_normalised(self):
        from config.roads import validate_road_prefab, KNOWN_ROAD_PREFABS
        # Legacy fabricated name from v1.3.x must snap to a real asphalt prefab.
        out = validate_road_prefab("RG_Road_Asphalt_8m")
        assert out in KNOWN_ROAD_PREFABS
        # Surface is preserved.
        assert "Asphalt" in out

    def test_legacy_gravel_name_snaps_to_gravel_family(self):
        from config.roads import validate_road_prefab
        out = validate_road_prefab("RG_Road_Gravel_4m")
        # Should land on a gravel-surface prefab (forest road or trail).
        assert out in ("RG_Road_Forest_01", "RG_TrailGravel_01")

    def test_legacy_dirt_name_snaps_to_dirt_family(self):
        from config.roads import validate_road_prefab
        out = validate_road_prefab("RG_Road_Dirt_2m")
        # Should land on a dirt-surface prefab.
        assert out in ("RG_Road_Dirt_01", "RG_Road_Dirt_02", "RG_TrailDirt_01")

    def test_unknown_prefab_falls_back_to_narrow_asphalt(self):
        from config.roads import validate_road_prefab
        # Anything we can't even pattern-match goes to the safe default.
        assert validate_road_prefab("totally_made_up") == "RG_Road_Asphalt_E_01_Narrow"

    def test_canonical_name_passes_through(self):
        from config.roads import validate_road_prefab
        assert validate_road_prefab("RG_Road_Asphalt_E_03") == "RG_Road_Asphalt_E_03"

    def test_fully_qualified_road_prefab_includes_atlas2_guid_and_subdir(self):
        """The fq: form must use the exact GUID + path from Atlas 2 p. 12."""
        from config.roads import fully_qualified_road_prefab
        # Asphalt prefab → Asphalt/ subdirectory + verified GUID.
        assert fully_qualified_road_prefab("RG_Road_Asphalt_E_03") == (
            "{8B67F44381CD2216}PrefabLibrary/Generators/Roads/Asphalt/"
            "RG_Road_Asphalt_E_03.et"
        )
        # Cobblestone prefab → Cobblestone/ subdirectory.
        assert fully_qualified_road_prefab("RG_Road_Cobblestone_01") == (
            "{ABC15429070F8391}PrefabLibrary/Generators/Roads/Cobblestone/"
            "RG_Road_Cobblestone_01.et"
        )
        # Forest/trail prefabs live under Dirt/ per Atlas 2 SCR_SHPPrefabDataList.
        assert fully_qualified_road_prefab("RG_Road_Forest_01") == (
            "{6C770E9E60A49C05}PrefabLibrary/Generators/Roads/Dirt/"
            "RG_Road_Forest_01.et"
        )
        assert fully_qualified_road_prefab("RG_TrailGravel_01") == (
            "{98DF2267CDD17758}PrefabLibrary/Generators/Roads/Dirt/"
            "RG_TrailGravel_01.et"
        )

    def test_fully_qualified_road_prefab_returns_none_for_unknown(self):
        from config.roads import fully_qualified_road_prefab
        assert fully_qualified_road_prefab("RG_Road_Asphalt_8m") is None


# ---------------------------------------------------------------------------
# Bootstrap entities (issue #81)
# ---------------------------------------------------------------------------

def _metadata_for_4km() -> dict:
    return {
        "heightmap": {"dimensions": "2049x2049", "grid_cell_size_m": 2.0},
        "elevation": {
            "min_elevation_m": 0,
            "max_elevation_m": 100,
            "height_scale": 0.03125,
            "height_offset": 0,
        },
        "input": {"bbox": {"south": 58.0, "north": 58.04, "west": 7.9, "east": 7.94}},
    }


class TestBootstrapEntities:
    def test_mandatory_bootstrap_keys_emit(self):
        """Every key in MANDATORY_BOOTSTRAP_KEYS lands in the managers layer."""
        from services.enfusion_project_generator import EnfusionProjectGenerator
        from config.enfusion import MANDATORY_BOOTSTRAP_KEYS, WORLD_PREFABS

        gen = EnfusionProjectGenerator(
            map_name="TestMap", metadata=_metadata_for_4km(),
        )
        out = gen._generate_managers_layer()

        for key in MANDATORY_BOOTSTRAP_KEYS:
            path = WORLD_PREFABS[key]
            assert path in out, (
                f"Bootstrap entity {key!r} ({path}) is not in the managers layer"
            )

    def test_world_prefab_tables_are_consistent(self):
        """v1.5.0: every WORLD_PREFABS entry that ships in the managers layer
        must also have a verified per-prefab GUID and class name."""
        from config.enfusion import (
            WORLD_PREFABS, WORLD_PREFAB_GUIDS, WORLD_PREFAB_CLASS,
            MANDATORY_BOOTSTRAP_KEYS,
        )
        for key in MANDATORY_BOOTSTRAP_KEYS:
            assert key in WORLD_PREFABS, f"{key} missing from WORLD_PREFABS"
            assert key in WORLD_PREFAB_GUIDS, f"{key} missing from WORLD_PREFAB_GUIDS"
            assert key in WORLD_PREFAB_CLASS, f"{key} missing from WORLD_PREFAB_CLASS"
            guid = WORLD_PREFAB_GUIDS[key]
            assert len(guid) == 16, f"{key} GUID {guid!r} is not 16 hex chars"
            assert all(c in "0123456789ABCDEF" for c in guid), (
                f"{key} GUID {guid!r} contains non-uppercase-hex chars"
            )

    def test_terrain_prefab_path_matches_reference(self):
        """Verified against Testprojekt/Worlds/testworld_Layers/default.layer
        (Workbench-saved reference, May 2026)."""
        from config.enfusion import WORLD_PREFABS, WORLD_PREFAB_GUIDS
        assert WORLD_PREFABS["terrain"] == (
            "Prefabs/World/DefaultWorld/GenericTerrain_Default.et"
        )
        assert WORLD_PREFAB_GUIDS["terrain"] == "221ABC927C672E4E"

    def test_ambient_prefab_is_in_managers_layer(self):
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km(),
            country_codes=["SE"],
        )
        out = gen._generate_managers_layer()
        # Sweden → Arland ambient variant.
        assert "AmbientSounds_Arland.et" in out

    def test_default_country_uses_arland_ambient_in_v150(self):
        """v1.5.0 routes every country to Arland because Everon's GUID isn't
        yet verified against a saved Workbench reference."""
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km(),
            country_codes=["FR"],  # not Nordic, but still routes to Arland
        )
        out = gen._generate_managers_layer()
        assert "AmbientSounds_Arland.et" in out

    def test_managers_layer_has_balanced_braces(self):
        """Every bootstrap entity is a flat ``ClassName : "{GUID}path" { coords … }``
        block. Brace mismatch would suggest a nesting bug."""
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap", metadata=_metadata_for_4km(),
        )
        out = gen._generate_managers_layer()
        assert out.count("{") == out.count("}"), (
            "Bootstrap entities should each be a flat block; brace mismatch "
            "suggests a nesting bug like the v1.1.0 road regression."
        )

    def test_no_legacy_addon_guid_prefix_in_layers(self):
        """Regression for issue #111: no generated layer file should embed
        the addon-level ARMA_REFORGER_GUID as a per-prefab inheritance GUID.

        Pre-1.5 builds used ``{58D0FB3206B6F859}Prefabs/...`` everywhere,
        which made Workbench fail to resolve every world prefab and left
        the terrain entity in a broken state that crashed NVTT on first
        paint stroke.
        """
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap", metadata=_metadata_for_4km(),
        )
        for layer in (
            gen._generate_default_layer(),
            gen._generate_managers_layer(),
        ):
            assert "{58D0FB3206B6F859}Prefabs/" not in layer, (
                "issue #111 regression: addon GUID used as prefab GUID"
            )

    def test_default_layer_omits_inline_terrain_grid_properties(self):
        """Pre-1.5 wrote TerrainGridSizeX/Z, GridCellSize, HeightScale,
        HeightOffset, CloseDistance* inline on GenericTerrainEntity. Those
        properties don't belong in the layer (they live in tileMap.conf);
        emitting them triggered the "Unknown keyword/data" cascade in
        Gunnar's error.log (issue #111).
        """
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap", metadata=_metadata_for_4km(),
        )
        layer = gen._generate_default_layer()
        for forbidden in (
            "TerrainGridSizeX", "TerrainGridSizeZ", "GridCellSize",
            "HeightScale", "HeightOffset",
            "CloseDistanceMax", "CloseDistanceBlend",
            "MiddleDistanceMax", "MiddleDistanceBlend",
        ):
            assert forbidden not in layer, (
                f"issue #111 regression: {forbidden} still emitted inline"
            )

    def test_default_layer_omits_unknown_environment_classes(self):
        """Pre-1.5 wrote a hand-built ``GenericWorldEntity { SkyPreset { … }
        PlanetPresets { … } SkyVolCloudsRenderer { … } OceanPreset { … } }``
        block. All four classes report as "Unknown class" in current
        Workbench (Gunnar's error.log). The saved-by-editor reference has
        no environment block at all — Workbench rebuilds it on save.
        """
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap", metadata=_metadata_for_4km(),
        )
        layer = gen._generate_default_layer()
        for forbidden in (
            "SkyPreset", "PlanetPresets", "SkyVolCloudsRenderer", "OceanPreset",
        ):
            assert forbidden not in layer, (
                f"issue #111 regression: {forbidden} still emitted"
            )

    def test_no_legacy_fabricated_prefab_names_anywhere(self):
        """Make sure the fabricated `_<width>m` road names from v1.3.x are
        gone everywhere they used to appear in config/road code paths."""
        from config import roads as _roads
        from config.roads import KNOWN_ROAD_PREFABS
        for prefab in KNOWN_ROAD_PREFABS:
            # No prefab should still match the legacy width-suffix pattern.
            assert not prefab.endswith("m") or prefab.endswith("_01") or prefab.endswith("_02"), (
                f"Legacy width-suffix prefab leaked into catalogue: {prefab}"
            )


# ---------------------------------------------------------------------------
# Surface assignments sidecar
# ---------------------------------------------------------------------------

class TestSurfaceAssignments:
    def test_road_emits_surface_assignment(self):
        """Roads that get descriptive names also record their expected
        surface mask in EnfusionProjectGenerator.surface_assignments."""
        from services.enfusion_project_generator import EnfusionProjectGenerator

        class _Identity:
            def transform_points(self, points, elevation_array=None):
                return [
                    {"x": round(p["x"] * 1000, 3), "y": 0.0, "z": round(p["y"] * 1000, 3)}
                    for p in points
                ]

        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata=_metadata_for_4km(),
            road_data={"roads": [{
                "osm_id": 1, "name": "Storgatan", "highway_type": "residential",
                "surface": "asphalt", "width_m": 5.0,
                "is_bridge": False, "is_tunnel": False,
                "enfusion_prefab": "RG_Road_Asphalt_E_01_DashedLine",
                "spline_points": [
                    {"x": 1.0, "y": 1.0, "z": 0},
                    {"x": 1.5, "y": 1.5, "z": 0},
                    {"x": 2.0, "y": 2.0, "z": 0},
                ],
                "point_count": 3,
            }]},
            transformer=_Identity(),
        )
        gen._reset_naming_state()
        gen._generate_roads_layer()

        # Exactly one mapping should have been recorded, surface=asphalt.
        assert "Road_Storgatan_Asphalt" in gen.surface_assignments
        assert gen.surface_assignments["Road_Storgatan_Asphalt"] == "asphalt"


# ---------------------------------------------------------------------------
# resolve_ambient_prefab helper
# ---------------------------------------------------------------------------

class TestResolveAmbientPrefab:
    def test_first_matching_country_wins(self):
        from config.enfusion import resolve_ambient_prefab
        # SE is mapped; FR isn't. Mixed list: SE should win even if FR is first.
        assert resolve_ambient_prefab(["FR", "SE"]).endswith("AmbientSounds_Arland.et")

    def test_no_countries_returns_default(self):
        """v1.5.0: default is Arland until we capture an Everon reference line."""
        from config.enfusion import resolve_ambient_prefab
        assert resolve_ambient_prefab(None).endswith("AmbientSounds_Arland.et")
        assert resolve_ambient_prefab([]).endswith("AmbientSounds_Arland.et")

    def test_unknown_country_falls_back_to_default(self):
        from config.enfusion import resolve_ambient_prefab
        assert resolve_ambient_prefab(["XX"]).endswith("AmbientSounds_Arland.et")
