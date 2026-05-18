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

from services.surface_mask_generator import (  # noqa: E402
    check_block_saturation,
    generate_surface_masks,
)


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


def _run_and_load(osm_data: dict, tmp_path: Path, surface: str) -> np.ndarray:
    """Run generate_surface_masks and return the named mask as a 2-D uint8 array.

    Returns a zero array if the mask was skipped (no meaningful coverage).
    """
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
    path = tmp_path / f"surface_{surface}.png"
    if not path.exists():
        return np.zeros((H, W), dtype=np.uint8)
    return np.array(Image.open(path).convert("L"))


def _run_with(osm_data: dict, tmp_path: Path) -> np.ndarray:
    """Run generate_surface_masks and return the asphalt mask as a 2-D uint8 array."""
    return _run_and_load(osm_data, tmp_path, "asphalt")


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


class TestGravelAndDirtMatchRoadProcessorClassification:
    """v1.5.11: gravel and dirt road masks now classify every OSM road feature
    through infer_road_surface() (matching the spline export), instead of the
    old fixed `highway in {track,path,footway,bridleway}` tag filter that
    dropped the dominant Swedish-rural case (`highway=service` with
    surface=gravel|unpaved|compacted)."""

    def test_service_road_outside_urban_paints_gravel(self, tmp_path: Path):
        """Per road_processor.infer_road_surface, 'service' outside urban → gravel."""
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "service")],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }
        gravel = _run_and_load(osm_data, tmp_path, "gravel")
        assert gravel.sum() > 0, (
            "A 'service' road outside any urban polygon must paint gravel — "
            "this is the dominant Swedish-rural case the v1.5.11 fix addresses."
        )

    def test_service_road_inside_urban_skips_gravel(self, tmp_path: Path):
        """Same service road inside residential polygon → asphalt, not gravel."""
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "service")],
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
        gravel = _run_and_load(osm_data, tmp_path, "gravel")
        asphalt = _run_and_load(osm_data, tmp_path, "asphalt")
        assert asphalt.sum() > 0, "service-in-urban must paint asphalt"
        assert gravel.sum() == 0, (
            f"service-in-urban must NOT also paint gravel; got {gravel.sum()} px"
        )

    def test_track_paints_gravel_not_dirt(self, tmp_path: Path):
        """highway=track defaults to gravel (rules.track_surface_default)."""
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "track")],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }
        gravel = _run_and_load(osm_data, tmp_path, "gravel")
        dirt = _run_and_load(osm_data, tmp_path, "dirt")
        assert gravel.sum() > 0, "highway=track must paint gravel"
        assert dirt.sum() == 0, (
            f"highway=track must NOT paint dirt; got {dirt.sum()} px"
        )

    def test_path_paints_dirt_not_gravel(self, tmp_path: Path):
        """highway=path is unconditionally classified as dirt — pre-v1.5.11
        it was wrongly painted into the gravel mask by the `gravel_types` list."""
        osm_data = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "path")],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }
        gravel = _run_and_load(osm_data, tmp_path, "gravel")
        dirt = _run_and_load(osm_data, tmp_path, "dirt")
        assert dirt.sum() > 0, "highway=path must paint dirt"
        assert gravel.sum() == 0, (
            f"highway=path must NOT paint gravel; got {gravel.sum()} px"
        )

    def test_service_with_explicit_gravel_surface_paints_gravel(self, tmp_path: Path):
        """OSM `surface=gravel` on a service road in urban context — the
        explicit surface tag wins over the urban-context default."""
        feature = _road_feature(15.001, 58.0015, 15.004, "service")
        feature["properties"]["surface"] = "gravel"
        osm_data = {
            "roads": {"type": "FeatureCollection", "features": [feature]},
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
        gravel = _run_and_load(osm_data, tmp_path, "gravel")
        asphalt = _run_and_load(osm_data, tmp_path, "asphalt")
        assert gravel.sum() > 0, "explicit surface=gravel must paint gravel"
        assert asphalt.sum() == 0, (
            f"explicit surface=gravel must NOT paint asphalt; got {asphalt.sum()} px"
        )


class TestSurfaceMasksSaveAtRequestedDimensions:
    """Issue #100: Workbench crashes in nvtt::CubeSurface::toGamma when surface
    weight masks are at vertex resolution (N+1) instead of face resolution (N).
    The caller in map_generator.py now passes (vertex - 1) as the target dims;
    this test pins the contract that the saved PNGs match what we request."""

    def test_masks_are_saved_at_face_resolution(self, tmp_path: Path):
        # Elevation at vertex resolution (N+1 = 257), masks requested at face
        # resolution (N = 256) — mirrors the production call site.
        vertex = 257
        face = vertex - 1
        elevation = np.full((vertex, vertex), 100.0, dtype=np.float32)

        # Minimum viable OSM data — a single short motorway so an asphalt mask
        # gets saved and we have at least one non-default mask file to check.
        osm = {
            "roads": {
                "type": "FeatureCollection",
                "features": [_road_feature(15.001, 58.0015, 15.004, "motorway")],
            },
            "water": _empty_collection(),
            "forests": _empty_collection(),
            "buildings": _empty_collection(),
            "land_use": _empty_collection(),
        }

        generate_surface_masks(
            elevation=elevation,
            osm_data=osm,
            bounds=BBOX,
            cell_size_m=1.0,
            output_dir=tmp_path,
            country_code="SE",
            heightmap_dimensions=(face, face),
        )

        saved = list(tmp_path.glob("surface_*.png"))
        assert saved, "Expected at least one surface_*.png to be written"
        for png_path in saved:
            with Image.open(png_path) as img:
                assert img.size == (face, face), (
                    f"{png_path.name} is {img.size}, expected ({face}, {face}). "
                    f"Vertex-resolution masks crash Workbench's NVTT bake on the "
                    f"first manual paint stroke (issue #100)."
                )


class TestBlockSaturationUsesFaceCellSize:
    """The block-saturation check must iterate with step = BLOCK_FACE_SIZE (32),
    not BLOCK_VERTEX_SIZE (33). Stepping by 33 misaligns the analysis windows
    against the Enfusion block grid (which tiles by 32 faces) and produces
    block coordinates that don't match what Workbench actually sees."""

    def test_six_surfaces_in_a_single_face_block_is_detected(self):
        # A 64×64 face-resolution canvas = exactly 4 Enfusion blocks (2×2 of
        # 32×32). Paint 6 distinct surfaces inside the top-left block only.
        h = w = 64
        masks = {}
        for i, name in enumerate(["a", "b", "c", "d", "e", "f"]):
            m = np.zeros((h, w), dtype=np.uint8)
            # Each surface gets a small strip inside block (0..32, 0..32).
            y0 = i * 4
            m[y0:y0 + 3, 4:28] = 200
            masks[name] = m

        result = check_block_saturation(masks)

        assert result["violations"] >= 1, (
            "6 surfaces inside a single 32-cell block must register as a "
            "saturation violation."
        )
        # And the reported block coordinates must be face-cell block coords —
        # i.e. block (0, 0), not the 33-stride coord (0, 0) which would also
        # falsely flag adjacent blocks.
        offending = [
            (d["block_x"], d["block_y"]) for d in result["details"]
        ]
        assert (0, 0) in offending, (
            f"Expected the (0,0) block to be flagged; got {offending}"
        )
