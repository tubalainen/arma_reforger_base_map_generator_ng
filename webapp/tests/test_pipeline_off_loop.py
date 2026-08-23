"""
Guards that the pipeline's CPU-bound steps stay off the asyncio event loop.

`run_generation` is a coroutine, so any synchronous call it makes blocks the
whole app for the duration: `/status` stops answering, the browser Activity Log
freezes, and every log line produced during the block is delivered in one burst
once it ends. With generation now fast (#170) that burst lands at or after
completion, which is what made the Activity Log look like it was missing most
of the run (#175).

Measured on a real 2.3 km generation before the fix: one `/status` poll took
5.87 s and 85% of all log entries arrived in the final two responses. After
moving these steps to `asyncio.to_thread`: no poll over 0.62 s, and the batch
delivered at "Generation complete!" fell from 83 entries to 7.

These are source-level assertions rather than behavioural ones — driving the
whole pipeline needs network and minutes — but they pin the exact regression:
someone dropping the `await asyncio.to_thread(...)` wrapper back to a direct
call.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

MAP_GENERATOR = WEBAPP_DIR / "services" / "map_generator.py"

# Every heavy synchronous worker `run_generation` drives. Each must be reached
# through asyncio.to_thread, never called directly from the coroutine.
OFF_LOOP_CALLEES = {
    "step_generate_heightmap",
    "step_generate_surface_masks",
    "step_extract_features",
    "reproject_satellite_to_terrain_crs",
    "validate_and_harden_rasters",
    "organize_export_structure",
    "_write_zip_archive",
}


@pytest.fixture(scope="module")
def run_generation_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(MAP_GENERATOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_generation":
            return node
    pytest.fail("run_generation not found in map_generator.py")


def _to_thread_targets(node: ast.AST) -> set[str]:
    """Names passed as the first argument to asyncio.to_thread(...)."""
    found: set[str] = set()
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "to_thread" and call.args:
            target = call.args[0]
            if isinstance(target, ast.Name):
                found.add(target.id)
            elif isinstance(target, ast.Attribute):
                found.add(target.attr)
    return found


def _direct_calls(node: ast.AST) -> set[str]:
    """Callee names invoked directly (not as a to_thread argument)."""
    inside_to_thread: set[int] = set()
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "to_thread":
            for arg in call.args:
                inside_to_thread.add(id(arg))

    names: set[str] = set()
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        if id(call.func) in inside_to_thread:
            continue
        f = call.func
        if isinstance(f, ast.Name):
            names.add(f.id)
        elif isinstance(f, ast.Attribute):
            names.add(f.attr)
    return names


class TestHeavyStepsRunOffTheLoop:
    @pytest.mark.parametrize("callee", sorted(OFF_LOOP_CALLEES))
    def test_step_is_dispatched_to_a_thread(self, callee, run_generation_node):
        assert callee in _to_thread_targets(run_generation_node), (
            f"{callee} must be invoked via `await asyncio.to_thread(...)` — "
            "calling it directly blocks the event loop and freezes /status"
        )

    @pytest.mark.parametrize("callee", sorted(OFF_LOOP_CALLEES))
    def test_step_is_never_called_directly(self, callee, run_generation_node):
        assert callee not in _direct_calls(run_generation_node), (
            f"{callee} is still called directly somewhere in run_generation; "
            "every call site has to go through asyncio.to_thread"
        )

    def test_enfusion_generation_stays_off_the_loop(self, run_generation_node):
        """The #170 fix — pinned here so it cannot quietly regress."""
        assert "generate_all" in _to_thread_targets(run_generation_node)

    def test_the_zip_writer_is_a_separate_function(self):
        """
        Zipping is the longest synchronous stretch and sits immediately before
        completion, so it has to be extractable into a thread rather than
        inlined in the coroutine.
        """
        tree = ast.parse(MAP_GENERATOR.read_text(encoding="utf-8"))
        fns = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert "_write_zip_archive" in fns
        # ...and it must be a plain def: asyncio.to_thread cannot run a coroutine.
        node = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_write_zip_archive"
        )
        assert isinstance(node, ast.FunctionDef), "_write_zip_archive must be sync"

    def test_no_zipfile_writing_left_inline(self, run_generation_node):
        """
        A ZipFile opened directly inside the coroutine would block again even
        with the helper present.
        """
        for call in (n for n in ast.walk(run_generation_node) if isinstance(n, ast.Call)):
            f = call.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            assert name != "ZipFile", (
                "zipfile.ZipFile is opened inline in run_generation — "
                "pack via _write_zip_archive in a worker thread instead"
            )
