"""
Structural guards for the browser Activity Log (issue #175).

The Activity Log markup lives inside ``#progress-overlay``, which is hidden the
moment a job finishes. While spline cleanup took 10-30 minutes that was
invisible — there was ample time to read the log as it streamed. Once #170 made
generation fast, the overlay started closing within seconds of the last line
arriving and the log became effectively unreadable in the GUI: users could only
get it from ``docker compose logs``.

The fix moves the log's DOM node into the results panel on completion. These
tests are deliberately structural rather than behavioural — there is no JS test
runner in this project — but they pin the pieces that must line up, so a future
refactor cannot silently take the log away again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

INDEX_HTML = WEBAPP_DIR / "static" / "index.html"
APP_JS = WEBAPP_DIR / "static" / "js" / "app.js"


@pytest.fixture(scope="module")
def index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


class TestActivityLogMarkup:
    def test_console_section_is_addressable(self, index_html):
        """`moveConsoleTo` needs a stable id to move."""
        assert 'id="console-section"' in index_html
        assert 'id="console-log"' in index_html

    def test_both_anchors_exist(self, index_html):
        """One home during the run, one after it finishes."""
        assert 'id="progress-log-anchor"' in index_html
        assert 'id="results-log-slot"' in index_html

    def test_the_log_starts_inside_the_progress_anchor(self, index_html):
        """
        At page load the log must already sit in the anchor it will be moved
        out of, so the first move is a no-op rather than a reparent that
        shifts the layout mid-run.
        """
        anchor = index_html.index('id="progress-log-anchor"')
        section = index_html.index('id="console-section"')
        assert anchor < section, "console-section must be nested in the anchor"

    def test_the_results_slot_is_inside_the_results_panel(self, index_html):
        panel = index_html.index('id="results-panel"')
        slot = index_html.index('id="results-log-slot"')
        # The results panel is the last major block before the closing scripts.
        assert panel < slot

    def test_copy_button_exists(self, index_html):
        """The log is what users paste into bug reports."""
        assert 'id="btn-copy-console"' in index_html


class TestActivityLogWiring:
    def test_move_helper_exists(self, app_js):
        assert "function moveConsoleTo(" in app_js

    def test_completion_moves_the_log_before_hiding_the_overlay(self, app_js):
        """
        Order matters: hideProgress() makes the whole overlay display:none, so
        the move has to happen first or the log is briefly (and confusingly)
        invisible.
        """
        block = app_js[app_js.index("if (job.status === 'completed')"):]
        block = block[: block.index("} else if")]
        assert "moveConsoleTo('results-log-slot')" in block
        assert block.index("moveConsoleTo('results-log-slot')") < block.index(
            "hideProgress()"
        )

    def test_a_new_run_pulls_the_log_back_into_the_overlay(self, app_js):
        block = app_js[app_js.index("function showProgress()"):]
        block = block[: block.index("function hideProgress()")]
        assert "moveConsoleTo('progress-log-anchor')" in block
        # ...and it must happen before the log is cleared, or the clear would
        # target a node that is still parented to the results panel.
        assert block.index("moveConsoleTo('progress-log-anchor')") < block.index(
            "clearConsoleLog()"
        )

    def test_failure_keeps_the_overlay_open(self, app_js):
        """
        A failed run is exactly when the log matters most, so the overlay must
        NOT be hidden — but the Generate button has to come back or the user is
        stuck with no way to retry.
        """
        block = app_js[app_js.index("} else if (job.status === 'failed')"):]
        block = block[: block.index("        } catch (err)")]
        assert "hideProgress()" not in block
        assert "btn-generate" in block and "disabled = false" in block

    def test_copy_handler_is_bound_and_has_a_fallback(self, app_js):
        assert "getElementById('btn-copy-console')" in app_js
        assert "consoleLogText()" in app_js
        # navigator.clipboard needs a secure context; plain HTTP deployments
        # must still be able to grab the text.
        block = app_js[app_js.index("getElementById('btn-copy-console')"):]
        block = block[: block.index("document.getElementById('btn-toggle-console')")]
        assert "navigator.clipboard.writeText" in block
        assert "getSelection" in block

    def test_console_log_text_joins_on_real_newlines(self, app_js):
        """
        Regression: an earlier patch wrote a literal line break into the source
        instead of the escape, which still parsed but produced a stray blank.
        """
        assert r".join('\n')" in app_js


class TestCacheBusting:
    def test_static_assets_are_versioned(self):
        """
        index.html and app.js changed, so browsers must be forced to refetch.
        main.py rewrites the asset URLs with ?v=APP_VERSION.
        """
        main_py = (WEBAPP_DIR / "main.py").read_text(encoding="utf-8")
        assert "app.js?v={APP_VERSION}" in main_py
        assert "style.css?v={APP_VERSION}" in main_py
