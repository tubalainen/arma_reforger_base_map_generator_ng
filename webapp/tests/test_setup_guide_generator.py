"""
Tests for the SetupGuideGenerator (Phase 1 / task G1 + G6 corrections).

G1: surfaces with 0% coverage must not produce setup-guide steps. Walking
    the user through importing an empty mask wastes time and breeds
    distrust of the rest of the guide.

G6: §5 (Roads) must reflect the new auto-attached RoadGeneratorEntity
    behavior — not the old "right-click each spline" workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))


def _metadata(surfaces_present, coverage_per_surface, road_count=0) -> dict:
    return {
        "heightmap": {
            "dimensions": "2049x2049",
            "grid_cell_size_m": 2.0,
            "terrain_size_m": 4096,
        },
        "elevation": {
            "min_elevation_m": 0,
            "max_elevation_m": 100,
            "height_scale": 0.03125,
            "height_offset": 0,
        },
        "surface_masks": {
            "count": len(surfaces_present),
            "surfaces": surfaces_present,
            "surfaces_present": surfaces_present,
            "coverage": {
                "per_surface": coverage_per_surface,
                "recommended_default": "grass",
                "recommended_default_material": "Grass_01.emat",
            },
            "block_saturation": {"violations": 0, "total_blocks": 0},
        },
        "roads": {
            "total_segments": road_count,
            "by_surface": {"asphalt": road_count} if road_count else {},
            "by_type": {"primary": road_count} if road_count else {},
        },
        "features": {},
        "satellite": {},
        "input": {"bbox": {"south": 0, "north": 1, "west": 0, "east": 1}},
        "enfusion_import": {"recommended_settings": {}},
        "coordinate_transform": {},
    }


class TestSurfaceCoverageFiltering:
    """G1 — 0%-coverage surfaces should not appear in the surface-painting phase."""

    def test_zero_coverage_surface_is_skipped(self, tmp_path):
        from services.setup_guide_generator import SetupGuideGenerator

        meta = _metadata(
            surfaces_present=["grass", "asphalt", "rock"],
            coverage_per_surface={
                "grass": {"percentage": 75.0},
                "asphalt": {"percentage": 25.0},
                "rock": {"percentage": 0.0},
            },
        )
        gen = SetupGuideGenerator("TestMap", meta)
        guide = gen._phase_surface_painting()

        # The 25% asphalt surface should still be walked through.
        assert "asphalt" in guide.lower()
        # The 0% rock surface must not have its own import step.
        assert "Import Rock" not in guide
        assert "rock_floor" not in guide  # double-check no stray refs

    def test_all_surfaces_present_when_all_have_coverage(self, tmp_path):
        from services.setup_guide_generator import SetupGuideGenerator

        meta = _metadata(
            surfaces_present=["grass", "asphalt", "rock"],
            coverage_per_surface={
                "grass": {"percentage": 60.0},
                "asphalt": {"percentage": 30.0},
                "rock": {"percentage": 10.0},
            },
        )
        gen = SetupGuideGenerator("TestMap", meta)
        guide = gen._phase_surface_painting()

        assert "Import Asphalt" in guide
        assert "Import Rock" in guide

    def test_missing_coverage_data_treated_as_zero(self, tmp_path):
        """A surface in surfaces_present but missing from coverage map -> skipped."""
        from services.setup_guide_generator import SetupGuideGenerator

        meta = _metadata(
            surfaces_present=["grass", "asphalt", "sand"],
            coverage_per_surface={
                "grass": {"percentage": 80.0},
                "asphalt": {"percentage": 20.0},
                # sand intentionally absent
            },
        )
        gen = SetupGuideGenerator("TestMap", meta)
        guide = gen._phase_surface_painting()

        assert "Import Sand" not in guide


class TestHeightmapImportStep:
    """Issue #120 + #130 — the Import Height Map dialog has labels
    ``Invert in X axis``, ``Invert in Z axis``, ``Disable blocks under
    height``, ``Resample heights``, ``Lowest source height is`` and
    ``Highest source height is``. ``Resample heights`` is UNCHECKED by
    default; the two source-height fields are disabled while it is off.
    The original failure mode (#120) was a user enabling Resample and
    accepting Lowest=0 / Highest=1, which silently flattens every
    imported heightmap into a 0–1 m strip — their heightmap.asc ranged
    from -1.98 m to +23.29 m but HeightMap.desc came out as
    ``ResampleMinHeight 0 / ResampleMaxHeight 1``. v1.5.11 also fixed
    issue #130: the guide previously named the dialog fields with
    invented labels (``Invert X Axis``, ``Min Height``, ``Max Height``,
    ``Resample to specified range``) that don't exist in Workbench,
    leaving users unable to find them."""

    def test_heightmap_phase_uses_real_workbench_field_labels(self):
        from services.setup_guide_generator import SetupGuideGenerator

        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}})
        meta["elevation"]["min_elevation_m"] = -1.981
        meta["elevation"]["max_elevation_m"] = 23.289

        guide = SetupGuideGenerator("TestMap", meta)._phase_terrain_creation()

        # The exact field labels Workbench shows the user (issue #130).
        assert "**Invert in X axis**" in guide
        assert "**Invert in Z axis**" in guide
        assert "**Disable blocks under height**" in guide
        assert "**Resample heights**" in guide
        assert "**Lowest source height is**" in guide
        assert "**Highest source height is**" in guide
        # The project's actual elevation range — not 0/1.
        assert "-1.981" in guide
        assert "23.289" in guide
        # The load-bearing instruction: keep Resample unchecked.
        assert "UNCHECKED" in guide

    def test_heightmap_phase_does_not_use_invented_labels(self):
        """Issue #130 regression — the previous wording invented labels
        (Min Height / Max Height / Resample to specified range / Invert X
        Axis) that don't exist in the Workbench dialog. If anyone
        reintroduces them, this test catches it."""
        from services.setup_guide_generator import SetupGuideGenerator

        guide = SetupGuideGenerator(
            "TestMap", _metadata(["grass"], {"grass": {"percentage": 100.0}})
        )._phase_terrain_creation()

        assert "Min Height" not in guide
        assert "Max Height" not in guide
        assert "Resample to specified range" not in guide
        assert "Invert X Axis" not in guide
        assert "Invert Z Axis" not in guide

    def test_heightmap_phase_warns_about_dialog_defaults(self):
        from services.setup_guide_generator import SetupGuideGenerator

        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}})
        guide = SetupGuideGenerator("TestMap", meta)._phase_terrain_creation()

        # The narrative that explains *why* enabling Resample is wrong
        # for .asc imports — without this, future-us will silently delete
        # the table thinking the explicit values are redundant.
        assert "#120" in guide
        assert "Resample heights" in guide


class TestRoadsPhaseGuide:
    """v1.2.3 — §5 documents manual generator attach (the v1.1.0 auto-attach
    nested ``${guid}...`` syntax was reverted because Workbench rejected it
    and hung at 4% on world load)."""

    def test_no_roads_skipped_message(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}}, road_count=0)
        guide = SetupGuideGenerator("TestMap", meta)._phase_roads()
        # v1.4.0 — bootstrap-entities phase was inserted, so Roads is now Phase 6.
        assert "Phase 6: Roads (Skipped)" in guide

    def test_with_roads_describes_manual_attach(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}}, road_count=12)
        guide = SetupGuideGenerator("TestMap", meta)._phase_roads()

        # Headline + body describe manual generator attach.
        assert "manual generator attach" in guide
        assert "Add Child Entity" in guide
        assert "RoadGeneratorEntity" in guide
        # Prefab hint format from the .layer comment is documented.
        assert "// prefab:" in guide
        # The auto-attach framing from v1.1.0–v1.2.2 must be gone.
        assert "Auto-attached" not in guide
        assert "already carries" not in guide


class TestDataSourcesAppendix:
    """Issue #75 — every generated SETUP_GUIDE.md must include a per-generation
    record of which APIs/datasets supplied the data, plus attribution."""

    def _meta_with_sources(
        self,
        elevation_source: str,
        elevation_res_m,
        satellite_source: str | None,
        feature_sources: dict | None,
        countries=("SE",),
    ) -> dict:
        m = _metadata(["grass"], {"grass": {"percentage": 100.0}})
        m["elevation"]["source"] = elevation_source
        m["elevation"]["resolution_m"] = elevation_res_m
        m["input"]["countries"] = list(countries)
        m["input"]["primary_country"] = countries[0]
        if satellite_source is not None:
            m["satellite"] = {"source": satellite_source, "dimensions": "8192x8192"}
        if feature_sources is not None:
            m["feature_sources"] = feature_sources
        return m

    def test_sweden_with_lantmateriet_lists_actual_providers(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = self._meta_with_sources(
            elevation_source="Lantmäteriet STAC Höjd (1 m)",
            elevation_res_m=1,
            satellite_source="Lantmäteriet STAC Bild (most recent orthophoto)",
            feature_sources={
                "roads": "OpenStreetMap (Overpass)",
                "buildings": "OpenStreetMap (Overpass)",
                "water": "Lantmäteriet Hydrografi + Marktäcke",
                "forests": "Lantmäteriet Marktäcke",
                "land_use": "Lantmäteriet Marktäcke",
            },
            countries=("SE",),
        )

        guide = SetupGuideGenerator("TestMap", meta)._appendix_data_sources()

        assert "Appendix D: Data Sources" in guide
        assert "Lantmäteriet STAC Höjd (1 m)" in guide
        assert "1 m resolution" in guide
        assert "Lantmäteriet STAC Bild" in guide
        assert "Lantmäteriet Hydrografi + Marktäcke" in guide
        assert "Lantmäteriet Marktäcke" in guide
        assert "OpenStreetMap (Overpass)" in guide
        # Attribution lines must fire for OSM AND Lantmäteriet.
        assert "Open Database License" in guide  # OSM ODbL
        assert "CC0" in guide  # Lantmäteriet
        # Sentinel-2 attribution must NOT appear when we used Lantmäteriet imagery.
        assert "Sentinel-2 Cloudless" not in guide
        # Natural Earth attribution is always present.
        assert "Natural Earth" in guide

    def test_global_osm_only_path_lists_osm_for_every_category(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = self._meta_with_sources(
            elevation_source="Copernicus DEM GLO-30 (AWS Open Data)",
            elevation_res_m=30,
            satellite_source="Sentinel-2 Cloudless (EOX)",
            feature_sources={
                "roads": "OpenStreetMap (Overpass)",
                "buildings": "OpenStreetMap (Overpass)",
                "water": "OpenStreetMap (Overpass)",
                "forests": "OpenStreetMap (Overpass)",
                "land_use": "OpenStreetMap (Overpass)",
            },
            countries=("US",),
        )

        guide = SetupGuideGenerator("TestMap", meta)._appendix_data_sources()

        assert "Copernicus DEM GLO-30" in guide
        assert "30 m resolution" in guide
        assert "Sentinel-2 Cloudless" in guide
        # All five feature categories report OSM.
        assert guide.count("OpenStreetMap (Overpass)") >= 5
        # Lantmäteriet attribution must NOT appear when nothing came from there.
        assert "Lantmäteriet" not in guide
        # Sentinel-2 + Copernicus + OSM attribution all present.
        assert "Sentinel data 2024" in guide
        assert "Copernicus Data License" in guide
        assert "Open Database License" in guide

    def test_appendix_renders_without_feature_sources_metadata(self):
        """Backwards compatibility: older metadata.json without `feature_sources`
        must still produce a valid appendix (just without the per-category table
        contents)."""
        from services.setup_guide_generator import SetupGuideGenerator
        meta = self._meta_with_sources(
            elevation_source="AWS COP30",
            elevation_res_m=30,
            satellite_source="Sentinel-2 Cloudless (EOX)",
            feature_sources=None,
        )

        guide = SetupGuideGenerator("TestMap", meta)._appendix_data_sources()
        assert "Appendix D: Data Sources" in guide
        assert "(no per-category source info recorded)" in guide

    def test_next_steps_renumbered_to_appendix_e(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}})
        gen = SetupGuideGenerator("TestMap", meta)
        assert "Appendix E: Next Steps" in gen._appendix_next_steps()

    def test_full_generate_includes_data_sources_section(self, tmp_path):
        """End-to-end: the new appendix must appear in the actual file written
        by `.generate()`, not just be callable in isolation."""
        from services.setup_guide_generator import SetupGuideGenerator
        meta = self._meta_with_sources(
            elevation_source="Lantmäteriet STAC Höjd (1 m)",
            elevation_res_m=1,
            satellite_source="Lantmäteriet STAC Bild (most recent orthophoto)",
            feature_sources={
                "roads": "OpenStreetMap (Overpass)",
                "buildings": "OpenStreetMap (Overpass)",
                "water": "Lantmäteriet Hydrografi",
                "forests": "Lantmäteriet Marktäcke",
                "land_use": "Lantmäteriet Marktäcke",
            },
            countries=("SE",),
        )
        meta["map_name"] = "TestMap"

        gen = SetupGuideGenerator("TestMap", meta)
        path = gen.generate(tmp_path)
        body = path.read_text(encoding="utf-8")

        assert "Appendix D: Data Sources" in body
        assert "Appendix E: Next Steps" in body
        # Data sources section appears before Next Steps in the rendered file.
        assert body.index("Appendix D: Data Sources") < body.index("Appendix E: Next Steps")


class TestBuildMetadataFeatureSources:
    """Issue #75 — build_metadata must propagate feature_sources into metadata.json."""

    def _minimal_results(self) -> dict:
        return {
            "polygon_coords": [[0, 0], [1, 0], [1, 1], [0, 1]],
            "options": {},
            "country_info": {
                "bbox": {"south": 0, "north": 1, "west": 0, "east": 1},
                "countries": ["SE"],
                "primary_country": "SE",
                "crs": "EPSG:3006",
            },
            "elevation_result": {"source": "Lantmäteriet STAC Höjd (1 m)", "resolution_m": 1},
            "heightmap_result": {
                "dimensions": "2049x2049",
                "terrain_grid_size": "2048x2048",
                "terrain_size_m": 4096,
                "grid_cell_size_m": 2.0,
                "min_elevation": 0,
                "max_elevation": 100,
                "height_scale": 0.03125,
                "dialog_height_scale": 0.03125,
                "height_offset": 0,
            },
            "surface_result": {"mask_count": 1, "surfaces": ["grass"]},
            "road_result": {"stats": {"total": 0, "by_surface": {}, "by_type": {}}},
            "features": {"summary": {}},
        }

    def test_feature_sources_round_trip(self):
        from services.map_generator import build_metadata
        sources = {
            "roads": "OpenStreetMap (Overpass)",
            "water": "Lantmäteriet Hydrografi",
            "forests": "Lantmäteriet Marktäcke",
        }
        meta = build_metadata(**self._minimal_results(), feature_sources=sources)
        assert meta["feature_sources"] == sources

    def test_no_feature_sources_omits_the_key(self):
        from services.map_generator import build_metadata
        meta = build_metadata(**self._minimal_results())
        assert "feature_sources" not in meta


class TestTroubleshootingAppendix:
    """Appendix C must walk users through the post-import reopen crash
    (issue #120). The chain is: maps generated before v1.5.7 lack the
    `GenericWorldEntity world {…}` block (env materials missing), and
    NVTT's cubemap baker null-derefs on the next world reload. The fix is
    to regenerate against v1.5.7+. The troubleshooting text must say so
    explicitly so a user hitting the crash on an old bundle knows which
    version banner to look for."""

    def _gen(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = _metadata(
            surfaces_present=["grass"],
            coverage_per_surface={"grass": {"percentage": 100.0}},
        )
        return SetupGuideGenerator("TestMap", meta)

    def test_issue_120_section_is_present(self):
        guide = self._gen()._appendix_troubleshooting()
        assert (
            "### Workbench crashes on world reopen after heightmap import "
            "(issue #120)"
        ) in guide

    def test_issue_120_section_cites_nvtt_signature_and_v157_fix(self):
        """The crash signature and the version cutoff are the two facts a
        user actually needs to triage their old bundle. Both must appear in
        the same section."""
        guide = self._gen()._appendix_troubleshooting()
        section_start = guide.index("(issue #120)")
        # Next H3 heading bounds the #120 section.
        next_heading = guide.find("\n### ", section_start + 1)
        section = guide[section_start:next_heading] if next_heading != -1 else guide[section_start:]
        assert "nvtt::CubeSurface::toGamma" in section, (
            "crash signature missing — users won't recognise their crash.log"
        )
        assert "v1.5.6 or earlier" in section, (
            "version cutoff missing — users won't know which banner to look for"
        )
        assert "v1.5.7" in section, (
            "v1.5.7 fix version missing — users won't know what to regenerate against"
        )
        assert "default.layer" in section, (
            "file pointer missing — users need to know where the banner lives"
        )

    def test_no_sky_section_reflects_v157_restoration(self):
        """The 'No sky/atmosphere' entry used to claim Workbench rebuilds the
        env block on save (v1.5.0-1.5.6). v1.5.7 emits it directly. The
        guide must not perpetuate the old 'Workbench rebuilds it' story."""
        guide = self._gen()._appendix_troubleshooting()
        assert "Workbench rebuilds it on first" not in guide
