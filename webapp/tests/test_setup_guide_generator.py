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


class TestRoadsPhaseGuide:
    """G6 — §5 must reflect the new auto-attach behavior."""

    def test_no_roads_skipped_message(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}}, road_count=0)
        guide = SetupGuideGenerator("TestMap", meta)._phase_roads()
        assert "Phase 5: Roads (Skipped)" in guide

    def test_with_roads_says_auto_attached(self):
        from services.setup_guide_generator import SetupGuideGenerator
        meta = _metadata(["grass"], {"grass": {"percentage": 100.0}}, road_count=12)
        guide = SetupGuideGenerator("TestMap", meta)._phase_roads()

        # Headline now describes auto-attached prefabs.
        assert "Auto-attached" in guide
        # Manual fallback path still documented for the rare break case.
        assert "Manual Fallback" in guide
        # The old "Manual Prefab Assignment Required" framing must be gone.
        assert "Manual Prefab Assignment Required" not in guide
