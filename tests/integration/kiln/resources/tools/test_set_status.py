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

from kiln.scheduler.infrastructure.terminal.pane_status import STATE_COLORS_HEX

SET_STATUS_PY = (
    Path(__file__).resolve().parents[5] / "src" / "kiln" / "resources" / "tools" / "set-status.py"
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

    def test_cycles_and_cost_are_included_when_given(self, set_status):
        status = set_status.build_status("coder", "working", None, "auto", cycles=7, cost_usd=1.25)
        assert status["cycles"] == 7
        assert status["cost_usd"] == 1.25

    def test_the_token_breakdown_is_kept(self, set_status):
        usage = {"input": 100, "output": 20, "cache_read": 900, "cache_write": 30}
        status = set_status.build_status("coder", "working", None, "auto", tokens=usage)
        assert status["token_usage"] == usage

    def test_the_total_is_derived_from_the_breakdown(self, set_status):
        # Derived here rather than accepted as its own flag, so the total and the breakdown
        # cannot disagree.
        usage = {"input": 100, "output": 20, "cache_read": 900, "cache_write": 30}
        status = set_status.build_status("coder", "working", None, "auto", tokens=usage)
        assert status["tokens"] == 1050

    def test_a_partial_breakdown_totals_only_what_was_reported(self, set_status):
        status = set_status.build_status("coder", "working", None, "auto", tokens={"output": 7})
        assert status["tokens"] == 7
        assert status["token_usage"] == {"output": 7}

    def test_cycles_cost_and_tokens_are_omitted_when_absent(self, set_status):
        # Not written as 0/None -- a wrapper-mode role that never tracked any of them must
        # not have its status file claim "$0.00 spent, 0 cycles, 0 tokens".
        status = set_status.build_status("coder", "working", None, "auto")
        assert "cycles" not in status
        assert "cost_usd" not in status
        assert "tokens" not in status
        assert "token_usage" not in status


class TestParseArgv:
    def test_role_and_state(self, set_status):
        parsed = set_status.parse_argv(["coder", "working"])
        assert parsed == ("coder", "working", None, "auto", None, None, None, {})

    def test_detail_is_captured(self, set_status):
        detail = set_status.parse_argv(["coder", "delegating", "coder-worker"])[2]
        assert detail == "coder-worker"

    def test_a_bare_dash_clears_detail(self, set_status):
        assert set_status.parse_argv(["coder", "working", "-"])[2] is None

    def test_mode_flag_is_parsed(self, set_status):
        assert set_status.parse_argv(["coder", "working", "--mode=manual"])[3] == "manual"

    def test_mode_flag_does_not_get_mistaken_for_detail(self, set_status):
        assert set_status.parse_argv(["coder", "working", "--mode=manual"])[2] is None

    def test_cycles_flag_is_parsed_as_an_int(self, set_status):
        assert set_status.parse_argv(["coder", "working", "--cycles=7"])[4] == 7

    def test_cost_flag_is_parsed_as_a_float(self, set_status):
        assert set_status.parse_argv(["coder", "working", "--cost=1.25"])[5] == pytest.approx(1.25)

    def test_each_token_kind_is_parsed_into_the_breakdown(self, set_status):
        tokens = set_status.parse_argv(
            [
                "coder",
                "working",
                "--tokens-in=10",
                "--tokens-out=20",
                "--tokens-cache-read=30",
                "--tokens-cache-write=40",
            ]
        )[6]
        assert tokens == {"input": 10, "output": 20, "cache_read": 30, "cache_write": 40}

    def test_only_the_kinds_passed_appear(self, set_status):
        assert set_status.parse_argv(["coder", "working", "--tokens-in=10"])[6] == {"input": 10}

    def test_no_token_flags_yields_none_not_an_empty_dict(self, set_status):
        # None is what makes build_status omit the keys entirely.
        assert set_status.parse_argv(["coder", "working"])[6] is None

    def test_token_flags_do_not_get_mistaken_for_detail(self, set_status):
        assert set_status.parse_argv(["coder", "working", "--tokens-in=4200"])[2] is None

    def test_all_flags_together(self, set_status):
        parsed = set_status.parse_argv(
            [
                "coder",
                "working",
                "-",
                "--mode=auto",
                "--cycles=3",
                "--cost=0.5",
                "--tokens-in=99",
                "--tokens-cache-read=1",
            ]
        )
        assert parsed == (
            "coder",
            "working",
            None,
            "auto",
            3,
            0.5,
            {"input": 99, "cache_read": 1},
            {},
        )

    def test_missing_arguments_raise(self, set_status):
        with pytest.raises(ValueError, match="Usage"):
            set_status.parse_argv(["coder"])


class TestModelFlag:
    """
    The resolved model, carried to anything that displays roles.

    It travels through the status file for the same reason `worker_timeout_sec` does: only
    the scheduler process knows it. `resolve_model` is the CLI flag, else the worker
    definition's frontmatter, else a backend default -- so a reader that consulted the
    profile instead would show nothing for a role whose model comes from frontmatter.
    """

    def test_the_model_reaches_the_status_file(self, set_status):
        _, _, _, _, _, _, _, extras = set_status.parse_argv(
            ["coder", "working", "--model=claude-sonnet-5"]
        )

        assert extras["model"] == "claude-sonnet-5"

    def test_an_empty_model_is_omitted_rather_than_written_blank(self, set_status):
        # Empty means "the CLI picks its own default" for copilot/codex/grok. An empty
        # string in the file reads as broken configuration; absence reads as unknown, which
        # is the truth.
        _, _, _, _, _, _, _, extras = set_status.parse_argv(["coder", "working", "--model="])

        assert "model" not in extras

    def test_a_role_never_told_a_model_has_no_key_at_all(self, set_status):
        status = set_status.build_status("coder", "working", None, "auto", extras={})

        assert "model" not in status

    def test_the_numeric_extras_still_parse_alongside_it(self, set_status):
        # The text flags are checked first now, so the int path must not have been shadowed.
        _, _, _, _, cycles, _, _, extras = set_status.parse_argv(
            ["coder", "working", "--cycles=3", "--worker-timeout=1800", "--model=sonnet"]
        )

        assert cycles == 3
        assert extras["worker_timeout_sec"] == 1800
        assert extras["model"] == "sonnet"


class TestEndToEnd:
    def test_a_previously_rejected_state_is_now_accepted(self, tmp_path, monkeypatch):
        # "halted" was rejected before STATE_EMOJIS gained parity with STATE_COLORS_HEX --
        # the whole point of the fix. Runs the real script as a subprocess, not the loaded
        # module, so this exercises exactly what role_scheduler.py's make_status_writer does.
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SET_STATUS_PY), "coder", "halted"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

        status = json.loads((tmp_path / ".kiln" / "status" / "coder.json").read_text())
        assert status["state"] == "halted"

    def test_writes_without_the_env_var_when_installed_in_a_project(self, tmp_path, monkeypatch):
        # KILN_PROJECT_DIR is exported only by the WezTerm backend, so under tmux or Windows
        # Terminal every call died with "environment variable not set" and .kiln/status/ was
        # never written -- silently emptying the dashboard's STATE column and the persisted
        # cycle/cost totals. Found on Linux, where tmux is the only fallback there is.
        monkeypatch.delenv("KILN_PROJECT_DIR", raising=False)
        tools = tmp_path / ".kiln" / "tools"
        tools.mkdir(parents=True)
        installed = tools / "set-status.py"
        installed.write_bytes(SET_STATUS_PY.read_bytes())

        result = subprocess.run(
            [sys.executable, str(installed), "coder", "working"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        status = json.loads((tmp_path / ".kiln" / "status" / "coder.json").read_text())
        assert status["state"] == "working"

    def test_env_var_still_wins_over_the_derived_path(self, tmp_path, monkeypatch):
        # The fallback must not override an explicit launcher-supplied location.
        elsewhere = tmp_path / "explicit"
        monkeypatch.setenv("KILN_PROJECT_DIR", str(elsewhere))
        tools = tmp_path / ".kiln" / "tools"
        tools.mkdir(parents=True)
        installed = tools / "set-status.py"
        installed.write_bytes(SET_STATUS_PY.read_bytes())

        subprocess.run(
            [sys.executable, str(installed), "coder", "working"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert (elsewhere / ".kiln" / "status" / "coder.json").exists()
        assert not (tmp_path / ".kiln" / "status" / "coder.json").exists()

    def test_an_unknown_state_still_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        result = subprocess.run(
            [sys.executable, str(SET_STATUS_PY), "coder", "nonsense"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 1
        assert not (tmp_path / ".kiln" / "status" / "coder.json").exists()

    def test_cycles_and_cost_flags_land_in_the_json(self, tmp_path, monkeypatch):
        # This is what role_scheduler.py's make_status_writer actually invokes -- the
        # dashboard reads these two fields straight out of the written JSON.
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        result = subprocess.run(
            [
                sys.executable,
                str(SET_STATUS_PY),
                "coder",
                "working",
                "-",
                "--cycles=4",
                "--cost=2.5",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

        status = json.loads((tmp_path / ".kiln" / "status" / "coder.json").read_text())
        assert status["cycles"] == 4
        assert status["cost_usd"] == 2.5


class TestMainInProcess:
    """Entrypoint branches measured in this process rather than a child interpreter."""

    def test_project_root_is_derived_from_installed_tool_path(
        self, set_status, tmp_path, monkeypatch
    ):
        tool = tmp_path / ".kiln" / "tools" / "set-status.py"
        tool.parent.mkdir(parents=True)
        tool.touch()
        monkeypatch.setattr(set_status, "__file__", str(tool))
        assert set_status.project_root_from_own_path() == str(tmp_path)

    def test_project_root_is_not_guessed_from_an_unexpected_path(
        self, set_status, tmp_path, monkeypatch
    ):
        tool = tmp_path / "tools" / "set-status.py"
        tool.parent.mkdir()
        tool.touch()
        monkeypatch.setattr(set_status, "__file__", str(tool))
        assert set_status.project_root_from_own_path() is None

    def test_writes_status_and_terminal_title(self, set_status, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(set_status.sys, "argv", ["set-status.py", "coder", "working"])

        set_status.main()

        payload = json.loads(
            (tmp_path / ".kiln" / "status" / "coder.json").read_text(encoding="utf-8")
        )
        assert payload["state"] == "working"

    def test_repeated_state_preserves_when_it_was_entered(self, set_status, tmp_path, monkeypatch):
        status_dir = tmp_path / ".kiln" / "status"
        status_dir.mkdir(parents=True)
        status_file = status_dir / "coder.json"
        status_file.write_text(
            json.dumps({"state": "waiting", "since": "2026-08-25T08:00:00Z"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(set_status.sys, "argv", ["set-status.py", "coder", "waiting"])

        set_status.main()

        payload = json.loads(status_file.read_text(encoding="utf-8"))
        assert payload["since"] == "2026-08-25T08:00:00Z"

    def test_state_transition_gets_a_new_timestamp(self, set_status, tmp_path, monkeypatch):
        status_dir = tmp_path / ".kiln" / "status"
        status_dir.mkdir(parents=True)
        status_file = status_dir / "coder.json"
        status_file.write_text(
            json.dumps({"state": "waiting", "since": "2020-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(set_status.sys, "argv", ["set-status.py", "coder", "working"])

        set_status.main()

        payload = json.loads(status_file.read_text(encoding="utf-8"))
        assert payload["since"] != "2020-01-01T00:00:00Z"

    def test_bad_arguments_exit_cleanly(self, set_status, monkeypatch, capsys):
        monkeypatch.setattr(set_status.sys, "argv", ["set-status.py", "coder"])
        with pytest.raises(SystemExit) as caught:
            set_status.main()
        assert caught.value.code == 1
        assert capsys.readouterr().err

    def test_invalid_state_exit_is_clean(self, set_status, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("KILN_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(set_status.sys, "argv", ["set-status.py", "coder", "not-a-state"])
        with pytest.raises(SystemExit) as caught:
            set_status.main()
        assert caught.value.code == 1
        assert "Error:" in capsys.readouterr().err

    def test_missing_project_location_exit_explains_environment(
        self, set_status, monkeypatch, capsys
    ):
        monkeypatch.delenv("KILN_PROJECT_DIR", raising=False)
        monkeypatch.setattr(set_status, "project_root_from_own_path", lambda: None)
        monkeypatch.setattr(set_status.sys, "argv", ["set-status.py", "coder", "working"])
        with pytest.raises(SystemExit) as caught:
            set_status.main()
        assert caught.value.code == 1
        assert "KILN_PROJECT_DIR" in capsys.readouterr().err
