"""
`set-status.py` writes `.kiln/status/<role>.json`, which drives the WezTerm tab-bar badge.

It's a standalone script (hyphenated filename, can't be `import`ed normally) copied verbatim
into every worktree by `workspace.copy_framework_tools()`, so it can't import
`scheduler.pane_status` at runtime -- its `STATE_EMOJIS` dict is a hand-maintained copy of
`pane_status.STATE_COLORS_HEX`'s keys. `test_state_vocabulary_matches_pane_status` is the
guard against those two drifting apart again the way they already did once (that drift meant
the WezTerm badge for a scheduler role could get silently stuck showing a stale state --
see role_scheduler.py's `_run_loop` fix in the same change as this test).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scheduler.pane_status import STATE_COLORS_HEX

SET_STATUS_PY = (
    Path(__file__).resolve().parents[1] / "kiln" / "framework" / "tools" / "set-status.py"
)

pytestmark = pytest.mark.integration


@pytest.fixture
def set_status():
    """Load set-status.py fresh -- its filename has a hyphen, so it can't be `import`ed."""
    spec = importlib.util.spec_from_file_location("set_status_under_test", SET_STATUS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestVocabularyParity:
    def test_state_vocabulary_matches_pane_status(self, set_status):
        assert set(set_status.STATE_EMOJIS) == set(STATE_COLORS_HEX)


class TestBuildStatus:
    @pytest.mark.parametrize("state", list(STATE_COLORS_HEX))
    def test_every_known_state_builds_a_status(self, set_status, state):
        status = set_status.build_status("coder", state, None, "auto")
        assert status["state"] == state
        assert status["role"] == "coder"
        assert state in status["title"]

    def test_an_unknown_state_raises(self, set_status):
        with pytest.raises(ValueError, match="unknown state"):
            set_status.build_status("coder", "nonsense", None, "auto")

    def test_detail_is_appended_to_the_title(self, set_status):
        status = set_status.build_status("specifier", "delegating", "specifier-worker", "auto")
        assert status["detail"] == "specifier-worker"
        assert "specifier-worker" in status["title"]

    def test_no_detail_is_fine(self, set_status):
        status = set_status.build_status("coder", "waiting", None, "auto")
        assert status["detail"] is None

    def test_mode_is_carried_through(self, set_status):
        assert set_status.build_status("coder", "waiting", None, "manual")["mode"] == "manual"


class TestParseArgv:
    def test_role_and_state(self, set_status):
        role, state, detail, mode = set_status.parse_argv(["coder", "working"])
        assert (role, state, detail, mode) == ("coder", "working", None, "auto")

    def test_detail_is_captured(self, set_status):
        _, _, detail, _ = set_status.parse_argv(["coder", "delegating", "coder-worker"])
        assert detail == "coder-worker"

    def test_a_bare_dash_clears_detail(self, set_status):
        _, _, detail, _ = set_status.parse_argv(["coder", "working", "-"])
        assert detail is None

    def test_mode_flag_is_parsed(self, set_status):
        _, _, _, mode = set_status.parse_argv(["coder", "working", "--mode=manual"])
        assert mode == "manual"

    def test_mode_flag_does_not_get_mistaken_for_detail(self, set_status):
        _, _, detail, _ = set_status.parse_argv(["coder", "working", "--mode=manual"])
        assert detail is None

    def test_missing_arguments_raise(self, set_status):
        with pytest.raises(ValueError, match="Usage"):
            set_status.parse_argv(["coder"])


class TestEndToEnd:
    def test_a_previously_rejected_state_is_now_accepted(self, tmp_path, monkeypatch):
        # "halted" was rejected before STATE_EMOJIS gained parity with STATE_COLORS_HEX --
        # the whole point of the fix. Runs the real script as a subprocess, not the loaded
        # module, so this exercises exactly what role_scheduler.py's make_status_writer does.
        monkeypatch.setenv("Kiln_PROJECT_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SET_STATUS_PY), "coder", "halted"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr

        status = json.loads((tmp_path / ".kiln" / "status" / "coder.json").read_text())
        assert status["state"] == "halted"

    def test_an_unknown_state_still_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("Kiln_PROJECT_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SET_STATUS_PY), "coder", "nonsense"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 1
        assert not (tmp_path / ".kiln" / "status" / "coder.json").exists()
