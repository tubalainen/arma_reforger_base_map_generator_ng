"""
The browser Activity Log must mirror `docker compose logs`.

Both are fed from the same `logging` records via services/job_log_handler.py,
so any pipeline stage that logs at INFO reaches the user's browser. These tests
pin that contract and the stages that were previously silent.
"""

import logging

import pytest

from services.job_log_handler import (
    current_job_var,
    install_job_log_handler,
)
from services.map_generator import MapGenerationJob


@pytest.fixture
def captured_job():
    """A job wired to the log tee; yields the job so tests can read job.logs."""
    install_job_log_handler(level=logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    job = MapGenerationJob("testjob", [], {}, "session")
    token = current_job_var.set(job)
    yield job
    current_job_var.reset(token)


def _messages(job):
    return [entry["message"] for entry in job.logs]


class TestLogTee:
    def test_service_logs_reach_the_job(self, captured_job):
        logging.getLogger("services.anything").info("hello from a service")
        assert "hello from a service" in _messages(captured_job)

    def test_third_party_noise_is_excluded(self, captured_job):
        """GDAL/rasterio/urllib3 chatter stays out of the user's view — it was
        deliberately suppressed in v1.5.6."""
        logging.getLogger("rasterio._env").info("CPLE_AppDefined blah")
        logging.getLogger("urllib3.connectionpool").info("Starting new HTTPS conn")
        assert _messages(captured_job) == []

    def test_levels_map_to_frontend_names(self, captured_job):
        logging.getLogger("services.x").warning("careful")
        logging.getLogger("services.x").error("broken")
        levels = [e["level"] for e in captured_job.logs]
        assert levels == ["warning", "error"]


class TestPreviouslySilentStages:
    """Each stage below used to log only when something changed, or only at
    DEBUG — so the user saw dead air while it worked."""

    def test_polygon_spline_cleanup_reports_even_a_no_op(self, captured_job):
        from services.spline_cleanup import normalize_polygons

        ring = [[[13.0, 55.0], [13.001, 55.0], [13.001, 55.001], [13.0, 55.0]]]
        feature = {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": ring},
            "properties": {"water_type": "lake"},
        }
        normalize_polygons([feature], "lake")
        assert any("Spline cleanup [lake]" in m for m in _messages(captured_job)), (
            "a no-op cleanup run must still report itself"
        )

    def test_polyline_spline_cleanup_reports_even_a_no_op(self, captured_job):
        from services.spline_cleanup import normalize_polylines

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[13.0, 55.0], [13.001, 55.0], [13.002, 55.0]],
            },
            "properties": {},
        }
        normalize_polylines([feature], "river")
        assert any("Spline cleanup [river]" in m for m in _messages(captured_job))

    def test_road_topology_reports_every_phase(self, captured_job):
        from services.road_topology import build_road_network

        ways = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [13.0 + i * 0.002, 55.0], [13.0 + (i + 1) * 0.002, 55.0]
                    ],
                },
                "properties": {"highway": "primary", "name": "E4"},
            }
            for i in range(4)
        ]
        build_road_network(ways)
        messages = " | ".join(_messages(captured_job))
        assert "Road stitching" in messages
        assert "Road junctions" in messages
        assert "Road densification" in messages

    def test_project_files_are_reported_as_they_are_written(self, captured_job, tmp_path):
        """_write_file logged at DEBUG, so the whole export phase was invisible."""
        from services.enfusion_project_generator import EnfusionProjectGenerator

        gen = EnfusionProjectGenerator(
            map_name="TestMap",
            metadata={
                "heightmap": {"dimensions": "2049x2049", "grid_cell_size_m": 2.0},
                "elevation": {
                    "min_elevation_m": 0, "max_elevation_m": 100,
                    "height_scale": 0.03125, "height_offset": 0,
                },
                "input": {"bbox": {"south": 0.0, "north": 4.0,
                                   "west": 0.0, "east": 4.0}},
            },
        )
        gen._reset_naming_state()
        gen._write_file(tmp_path / "default.layer", "GenericWorldEntity world {\n}\n")

        assert any(
            "Wrote default.layer" in m for m in _messages(captured_job)
        ), "each generated project file must be reported"
