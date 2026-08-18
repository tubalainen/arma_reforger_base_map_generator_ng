"""
Tests for services/road_topology.py — issue #161.

The reporter's complaint: road splines stop in the middle of a road with a
visible seam where the next one starts, there is no main-road/side-road
relationship, and sparse control points make roads cut into or fly over the
terrain.
"""

import pytest

from services.road_topology import (
    build_road_network,
    densify,
    metres_between,
    road_rank,
    snap_junctions,
    stitch_ways,
)


def _way(coords, **props):
    props.setdefault("highway", "residential")
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
        "properties": props,
    }


class TestStitchWays:
    def test_two_fragments_of_one_road_become_one(self):
        """The core #161 defect: OSM splits a road at every tag change."""
        ways = [
            _way([[13.0, 55.0], [13.001, 55.0]], name="Storgatan"),
            _way([[13.001, 55.0], [13.002, 55.0]], name="Storgatan"),
        ]
        merged = stitch_ways(ways)
        assert len(merged) == 1
        coords = merged[0]["geometry"]["coordinates"]
        assert len(coords) == 3, "shared node must not be duplicated"
        assert coords[0] == [13.0, 55.0]
        assert coords[-1] == [13.002, 55.0]
        assert merged[0]["properties"]["merged_way_count"] == 2

    def test_fragment_stored_reversed_still_merges(self):
        """OSM way direction is arbitrary; a reversed fragment must still join."""
        ways = [
            _way([[13.0, 55.0], [13.001, 55.0]], name="Storgatan"),
            _way([[13.002, 55.0], [13.001, 55.0]], name="Storgatan"),
        ]
        merged = stitch_ways(ways)
        assert len(merged) == 1
        assert merged[0]["geometry"]["coordinates"][-1] == [13.002, 55.0]

    def test_t_junction_is_not_merged_through(self):
        """Three way-ends at a node is a real junction — roads must end there."""
        ways = [
            _way([[13.0, 55.0], [13.001, 55.0]], name="Storgatan"),
            _way([[13.001, 55.0], [13.002, 55.0]], name="Storgatan"),
            _way([[13.001, 55.0], [13.001, 55.001]], name="Sidogatan"),
        ]
        merged = stitch_ways(ways)
        assert len(merged) == 3, "a T-junction must not be welded together"

    def test_incompatible_attributes_do_not_merge(self):
        """A merged road maps to one prefab and width, so tags must agree."""
        ways = [
            _way([[13.0, 55.0], [13.001, 55.0]], surface="asphalt"),
            _way([[13.001, 55.0], [13.002, 55.0]], surface="gravel"),
        ]
        assert len(stitch_ways(ways)) == 2

    def test_non_linestring_features_pass_through(self):
        point = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [13.0, 55.0]},
            "properties": {},
        }
        assert len(stitch_ways([point])) == 1

    def test_empty_input_is_safe(self):
        assert stitch_ways([]) == []


class TestRanking:
    def test_main_roads_outrank_side_roads(self):
        assert road_rank("motorway") < road_rank("primary") < road_rank("residential")
        assert road_rank("residential") < road_rank("track")

    def test_unknown_type_gets_the_default(self):
        assert road_rank("banana") == road_rank(None) == 8


class TestSnapJunctions:
    def test_side_road_end_snaps_onto_the_main_road(self):
        """A few metres of OSM slack renders as a floating stub in the editor."""
        main = _way(
            [[13.0, 55.0], [13.001, 55.0], [13.002, 55.0]], highway="primary"
        )
        # Side road ends ~4 m short of the main road's middle vertex.
        side = _way(
            [[13.001, 55.0005], [13.001, 55.00025], [13.001, 55.00004]],
            highway="residential",
        )
        snapped = snap_junctions([main, side], tolerance_m=12.0)
        assert snapped == 1
        assert side["geometry"]["coordinates"][-1] == [13.001, 55.0]

    def test_main_road_is_never_moved_to_fit_a_side_road(self):
        main = _way(
            [[13.0, 55.0], [13.001, 55.0], [13.002, 55.0]], highway="primary"
        )
        side = _way(
            [[13.001, 55.0005], [13.001, 55.00025], [13.001, 55.00004]],
            highway="residential",
        )
        before = [list(c) for c in main["geometry"]["coordinates"]]
        snap_junctions([main, side], tolerance_m=12.0)
        assert main["geometry"]["coordinates"] == before

    def test_two_point_side_road_can_snap(self):
        """A straight two-point stub is the most common side road in OSM; an
        earlier guard required three points and silently skipped them."""
        main = _way(
            [[13.0, 55.0], [13.004, 55.0], [13.008, 55.0]], highway="primary"
        )
        side = _way([[13.004, 55.000045], [13.004, 55.002]],
                    highway="residential")
        assert snap_junctions([main, side], tolerance_m=12.0) == 1
        assert side["geometry"]["coordinates"][0] == [13.004, 55.0]

    def test_snap_never_collapses_a_segment(self):
        """Snapping must not pull an end onto (or past) its own neighbour."""
        main = _way([[13.0, 55.0], [13.001, 55.0]], highway="primary")
        side = _way([[13.0, 55.0000001], [13.000001, 55.0000002]],
                    highway="residential")
        before = [list(c) for c in side["geometry"]["coordinates"]]
        snap_junctions([main, side], tolerance_m=12.0)
        assert side["geometry"]["coordinates"] == before

    def test_far_apart_roads_are_left_alone(self):
        main = _way([[13.0, 55.0], [13.002, 55.0]], highway="primary")
        side = _way(
            [[13.001, 55.01], [13.001, 55.011], [13.001, 55.012]],
            highway="residential",
        )
        assert snap_junctions([main, side], tolerance_m=12.0) == 0


class TestDensify:
    def test_long_segment_gains_intermediate_points(self):
        """Enfusion interpolates straight between control points, so a sparse
        spline cuts into rising ground (#161)."""
        # ~110 m north-south.
        coords = [[13.0, 55.0], [13.0, 55.001]]
        out = densify(coords, max_spacing_m=8.0)
        assert len(out) > 10
        gaps = [metres_between(out[i], out[i + 1]) for i in range(len(out) - 1)]
        assert max(gaps) <= 8.5, "no segment may exceed the target spacing"

    def test_endpoints_are_preserved_exactly(self):
        coords = [[13.0, 55.0], [13.0, 55.001]]
        out = densify(coords, max_spacing_m=8.0)
        assert out[0] == [13.0, 55.0]
        assert out[-1] == [13.0, 55.001]

    def test_short_segments_are_left_alone(self):
        coords = [[13.0, 55.0], [13.00001, 55.0]]
        assert densify(coords, max_spacing_m=8.0) == coords

    def test_point_budget_is_not_exceeded(self):
        coords = [[13.0, 55.0], [13.0, 55.5]]  # ~55 km
        out = densify(coords, max_spacing_m=1.0, max_points=500)
        assert len(out) <= 500


class TestBuildRoadNetwork:
    def test_full_pipeline_reports_stats(self):
        ways = [
            _way([[13.0, 55.0], [13.001, 55.0]], name="Storgatan"),
            _way([[13.001, 55.0], [13.002, 55.0]], name="Storgatan"),
        ]
        merged, stats = build_road_network(ways)
        assert len(merged) == 1
        assert stats["input_ways"] == 2
        assert stats["merged_roads"] == 1
        assert stats["points_after"] > stats["points_before"], (
            "densification must add control points for terrain following"
        )

    def test_malformed_input_never_loses_roads(self):
        ways = [_way([[13.0, 55.0], [13.001, 55.0]])]
        merged, stats = build_road_network(ways)
        assert len(merged) >= 1
        assert "error" not in stats
