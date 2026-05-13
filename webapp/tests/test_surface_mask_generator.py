"""
Tests for services/surface_mask_generator.py.

Focuses on the asphalt mask, which was rewritten in v1.2.5 to fix issue #55:
- Per-feature OSM widths drive per-feature pixel widths (so the mask matches
  the road splines emitted by road_processor).
- Urban landuse polygons and building footprints no longer paint asphalt
  outside actual roads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from services.surface_mask_generator import generate_surface_masks  # noqa: E402


# Small Swedish bbox at ~1m/pixel — 512×512 is enough for the rasterization
# logic without blowing up runtime.
BBOX = (15.0, 58.0, 15.005, 58.003)  # roughly 300m × 333m at this latitude
W, H = 512, 384


def _flat_elevation() -> np.ndarray:
    return np.full((H, W), 100.0, dtype=np.float32)


def _road_feature(lng_start: float, lat: float, lng_end: float, highway: str,
                  width: str | None = None, lanes: str | None = None) -> dict:
    props = {"highway": highway}
    if width is not None:
        props["width"] = width
    if lanes is not None:
        props["lanes"] = lanes
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[lng_start, lat], [lng_end, lat]],
        },
        "properties": props,
    }


def _landuse_polygon(lng_min: float, lat_min: float, lng_max: float, lat_max: float,
                     landuse_type: str) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lng_min, lat_min], [lng_max, lat_min],
                [lng_max, lat_max], [lng_min, lat_max], [lng_min, lat_min],
            ]],
        },
        "properties": {"type": landuse_type},
    }


def _building_polygon(lng_min: float, lat_min: float, lng_max: float, lat_max: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lng_min, lat_min], [lng_max, lat_min],
                [lng_max, lat_max], [lng_min, lat_max], [lng_min, lat_min],
            ]],
        },
        "properties": {},
    }


def _empty_collection() -> dict:
    return {"type": "FeatureCollection", "features": []}


def _run_with(osm_data: dict, tmp_path: Path) -> np.ndarray:
    """Run generate_surface_masks and return the asphalt mask as a 2-D uint8 array."""
    result = generate_surface_masks(
        elevation=_flat_elevation(),
        osm_data=osm_data,
        bounds=BBOX,
        cell_size_m=1.0,
        output_dir=tmp_path,
        country_code="SE",
        heightmap_dimensions=(W, H),
    )
    assert result is not None
    asphalt_path = tmp_path / "surface_asphalt.png"
    if not asphalt_path.exists():
        # The generator skips empty masks; return a zero array.
        return np.zeros((H, W), dtype=np.uint8)
    return np.array(Image.open(asphalt_path).convert("L"))


class TestAsphaltUrbanPolygonsNoLongerPainted:
    def test_residential_landuse_alone_produces_no_asphalt(self, tmp_path: Path):
        """Pre-v1.2.5: a residential polygon painted ~50% asphalt over its whole area.
        Post-v1.2.5: no roads → no asphalt mask file (or fully zero)."""
        osm_data = {
            "roads": _empty_collection(),
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": {
                "type": "FeatureCollection",
                "features": [
                    _landuse_polygon(15.001, 58.0005, 15.004, 58.0025, "residential"),
                ],
            },
        }
        mask = _run_with(osm_data, tmp_path)
        # Inside the residential polygon there must be zero asphalt.
        assert mask.sum() == 0, (
            f"Residential landuse with no roads must produce no asphalt; "
            f"got {mask.sum()} non-zero pixels."
        )

    def test_buildings_alone_produce_no_asphalt(self, tmp_path: Path):
        osm_data = {
            "roads": _empty_collection(),
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": {
                "type": "FeatureCollection",
                "features": [
                    _building_polygon(15.002, 58.001, 15.003, 58.002),
                ],
            },
            "land_use": _empty_collection(),
        }
        mask = _run_with(osm_data, tmp_path)
        assert mask.sum() == 0, (
            f"Building footprints with no roads must produce no asphalt; "
            f"got {mask.sum()} non-zero pixels."
        )


class TestAsphaltMatchesRoadProcessorClassification:
    def test_service_road_outside_urban_is_not_asphalt(self, tmp_path: Path):
        """Per road_processor.infer_road_surface, 'service' outside urban → gravel."""
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [
                    _road_feature(15.001, 58.0015, 15.004, "service"),
                ],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }
        mask = _run_with(osm_data, tmp_path)
        assert mask.sum() == 0, (
            "A 'service' road outside any urban polygon must be classified as "
            "gravel (not asphalt) so the mask agrees with the spline."
        )

    def test_service_road_inside_urban_is_asphalt(self, tmp_path: Path):
        """Same service road, but the midpoint sits inside a residential polygon."""
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [
                    _road_feature(15.001, 58.0015, 15.004, "service"),
                ],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": {
                "type": "FeatureCollection",
                "features": [
                    _landuse_polygon(15.0005, 58.0005, 15.0045, 58.0025, "residential"),
                ],
            },
        }
        mask = _run_with(osm_data, tmp_path)
        assert mask.sum() > 0, (
            "A 'service' road inside a residential polygon must be classified "
            "as asphalt (matching road_processor)."
        )

    def test_motorway_is_always_asphalt(self, tmp_path: Path):
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [
                    _road_feature(15.001, 58.0015, 15.004, "motorway"),
                ],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }
        mask = _run_with(osm_data, tmp_path)
        assert mask.sum() > 0


class TestAsphaltUsesPerFeatureWidth:
    def test_wider_road_paints_more_pixels(self, tmp_path: Path):
        """A 14m road should paint strictly more asphalt pixels than a 4m road."""
        # Run twice with the same road geometry but different OSM widths.
        narrow_osm = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "primary", width="4")],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }
        wide_osm = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "primary", width="14")],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }

        narrow_dir = tmp_path / "narrow"
        wide_dir = tmp_path / "wide"
        narrow_dir.mkdir()
        wide_dir.mkdir()

        narrow_mask = _run_with(narrow_osm, narrow_dir)
        wide_mask = _run_with(wide_osm, wide_dir)

        narrow_count = int((narrow_mask > 0).sum())
        wide_count = int((wide_mask > 0).sum())

        assert narrow_count > 0 and wide_count > 0
        # 14m / 4m ≈ 3.5×. Allow a generous tolerance for soft-edge blending.
        assert wide_count >= narrow_count * 1.8, (
            f"Wider road must produce a thicker asphalt stripe; "
            f"narrow(4m)={narrow_count}, wide(14m)={wide_count}. "
            f"Pre-v1.2.5 both used a fixed 6m buffer regardless of OSM width."
        )
