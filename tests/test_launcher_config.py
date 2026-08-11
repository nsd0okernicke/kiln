"""
Profile parsing. A bad profile must fail loudly at launch — a role silently dropped or
routed to the wrong agent is far more expensive to diagnose once panes are running.
"""

from __future__ import annotations

import json
import os

import pytest
from launcher import config
from launcher.config import (
    Profile,
    ProfileError,
    RoleConfig,
    default_profile_name,
    find_profiles_config,
    list_profiles,
    load_profile,
    parse_profile,
)

CONFIG = {
    "version": "1.0",
    "default": "compact",
    "profiles": {
        "compact": {
            "description": "Compact layout",
            "terminals": [
                {
                    "role": "specifier",
                    "agent": "claude",
                    "worktree": "@current",
                    "title": "Kiln Specifier",
                    "mode": "manual",
                    "model": "claude-sonnet-5",
                },
                {"role": "coder", "agent": "copilot", "worktree": "coder", "mode": "auto"},
            ],
            "layout": {"tabs": [{"title": "All Roles"}]},
        },
        "solo": {"terminals": [{"role": "coder"}]},
    },
}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "kiln").mkdir(parents=True)
    (root / "kiln" / "profiles.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    return root


class TestRoleConfig:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            ("coder", "Coder"),
            ("human-in-the-loop", "Human In The Loop"),
            ("some_role", "Some Role"),
        ],
    )
    def test_display_name(self, role, expected):
        assert RoleConfig(role=role).display_name == expected

    def test_session_name(self):
        assert RoleConfig(role="coder").session_name == "kiln-coder"

    @pytest.mark.parametrize("alias", ["@current", "none", "master"])
    def test_current_dir_aliases(self, alias):
        assert RoleConfig(role="specifier", worktree=alias).uses_current_dir is True

    def test_named_worktree_is_not_current_dir(self):
        assert RoleConfig(role="coder", worktree="coder").uses_current_dir is False

    def test_branch_name_for_worktree_role(self):
        assert RoleConfig(role="coder", worktree="coder").branch_name("main") == "main-coder"

    def test_branch_name_for_current_dir_role(self):
        # A @current role works on the base branch itself, not a sub-branch.
        assert RoleConfig(role="spec", worktree="@current").branch_name("main") == "main"

    def test_defaults_match_the_powershell_original(self):
        role = RoleConfig(role="coder")
        assert (role.agent, role.mode, role.worktree) == ("claude", "auto", "@current")
        assert role.scheduler is None


class TestSchedulerOptIn:
    def test_absent_flag_keeps_the_llm_wrapper(self):
        assert RoleConfig(role="coder").uses_scheduler is False

    def test_auto_role_with_flag_uses_the_scheduler(self):
        assert RoleConfig(role="coder", scheduler="python", mode="auto").uses_scheduler is True

    def test_manual_role_never_uses_the_scheduler(self):
        # Manual roles hold real conversations and need per-cycle human approval.
        role = RoleConfig(role="specifier", scheduler="python", mode="manual")
        assert role.uses_scheduler is False

    def test_unknown_scheduler_value_is_rejected(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "scheduler": "magic"}]}}}
        with pytest.raises(ProfileError, match="unsupported scheduler"):
            parse_profile(config, "p")

    def test_scheduler_accepted_for_claude(self):
        config = {
            "profiles": {"p": {"terminals": [{"role": "coder", "scheduler": "python"}]}}
        }
        assert parse_profile(config, "p").roles[0].uses_scheduler is True

    @pytest.mark.parametrize("agent", ["copilot", "codex", "grok"])
    def test_scheduler_accepted_for_backends_with_an_adapter(self, agent):
        config = {
            "profiles": {
                "p": {"terminals": [{"role": "coder", "agent": agent, "scheduler": "python"}]}
            }
        }
        assert parse_profile(config, "p").roles[0].uses_scheduler is True

    def test_scheduler_rejected_for_a_backend_with_no_adapter(self, monkeypatch):
        # Every currently-accepted agent has an adapter now, so this exercises the guard the
        # same way a future VALID_AGENTS addition without one yet would trip it -- by
        # temporarily excluding a real agent from SCHEDULER_CAPABLE_AGENTS rather than
        # asserting on a fictional one.
        monkeypatch.setattr(config, "SCHEDULER_CAPABLE_AGENTS", ("claude",))
        cfg = {
            "profiles": {
                "p": {"terminals": [{"role": "coder", "agent": "codex", "scheduler": "python"}]}
            }
        }
        with pytest.raises(ProfileError, match="no one-shot adapter"):
            parse_profile(cfg, "p")


class TestPassivePanes:
    """Roles that run no agent at all: inbox and dashboard. See RoleConfig.is_passive."""

    def test_inbox_role(self):
        role = RoleConfig(role="inbox", scheduler="inbox")
        assert role.is_inbox is True
        assert role.is_dashboard is False
        assert role.is_passive is True

    def test_dashboard_role(self):
        role = RoleConfig(role="dashboard", scheduler="dashboard")
        assert role.is_inbox is False
        assert role.is_dashboard is True
        assert role.is_passive is True

    def test_a_normal_role_is_neither(self):
        role = RoleConfig(role="coder")
        assert role.is_inbox is False
        assert role.is_dashboard is False
        assert role.is_passive is False

    def test_a_scheduler_role_is_not_passive(self):
        # uses_scheduler and is_passive are different axes -- a scheduler-driven role still
        # runs a real worker, it just isn't an interactive wrapper session.
        role = RoleConfig(role="coder", scheduler="python", mode="auto")
        assert role.is_passive is False

    def test_dashboard_is_accepted_by_profile_parsing(self):
        config = {
            "profiles": {"p": {"terminals": [{"role": "dashboard", "scheduler": "dashboard"}]}}
        }
        assert parse_profile(config, "p").roles[0].is_dashboard is True


class TestParsing:
    def test_parses_all_role_fields(self):
        role = parse_profile(CONFIG, "compact").roles[0]
        assert role.role == "specifier"
        assert role.agent == "claude"
        assert role.mode == "manual"
        assert role.model == "claude-sonnet-5"
        assert role.title == "Kiln Specifier"

    def test_preserves_role_order(self):
        assert [r.role for r in parse_profile(CONFIG, "compact").roles] == ["specifier", "coder"]

    def test_keeps_the_layout(self):
        assert parse_profile(CONFIG, "compact").layout["tabs"][0]["title"] == "All Roles"

    def test_applies_defaults_to_a_minimal_entry(self):
        role = parse_profile(CONFIG, "solo").roles[0]
        assert (role.agent, role.mode, role.worktree) == ("claude", "auto", "@current")
        assert role.worker_debug is False

    def test_worker_debug_opts_in(self):
        config = {
            "profiles": {"p": {"terminals": [{"role": "coder", "workerDebug": True}]}}
        }
        assert parse_profile(config, "p").roles[0].worker_debug is True

    def test_unknown_profile_lists_the_alternatives(self):
        with pytest.raises(ProfileError, match="compact, solo"):
            parse_profile(CONFIG, "nope")

    def test_duplicate_role_is_rejected(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder"}, {"role": "coder"}]}}}
        with pytest.raises(ProfileError, match="duplicate role 'coder'"):
            parse_profile(config, "p")

    def test_unsupported_agent_is_rejected(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "gpt"}]}}}
        with pytest.raises(ProfileError, match="unsupported agent 'gpt'"):
            parse_profile(config, "p")

    def test_grok_is_accepted_even_though_unimplemented(self):
        # Existing profiles reference it; config validation is not the place to break them.
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "grok"}]}}}
        assert parse_profile(config, "p").roles[0].agent == "grok"

    def test_unsupported_mode_is_rejected(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "mode": "semi"}]}}}
        with pytest.raises(ProfileError, match="unsupported mode 'semi'"):
            parse_profile(config, "p")

    def test_entry_without_a_role_is_rejected(self):
        config = {"profiles": {"p": {"terminals": [{"agent": "claude"}]}}}
        with pytest.raises(ProfileError, match="no 'role'"):
            parse_profile(config, "p")

    def test_profile_without_terminals_is_rejected(self):
        with pytest.raises(ProfileError, match="defines no terminals"):
            parse_profile({"profiles": {"p": {"terminals": []}}}, "p")


class TestProfileQueries:
    def test_finds_the_current_dir_role(self):
        assert parse_profile(CONFIG, "compact").current_dir_role.role == "specifier"

    def test_current_dir_role_is_none_when_all_roles_use_worktrees(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "worktree": "coder"}]}}}
        assert parse_profile(config, "p").current_dir_role is None

    def test_role_lookup(self):
        profile = parse_profile(CONFIG, "compact")
        assert profile.role("coder").agent == "copilot"
        assert profile.role("absent") is None

    def test_has_agent(self):
        profile = parse_profile(CONFIG, "compact")
        assert profile.has_agent("copilot") is True
        assert profile.has_agent("codex") is False

    def test_inbox_watches(self):
        config = {
            "profiles": {
                "p": {
                    "terminals": [
                        {"role": "human-in-the-loop", "mode": "manual"},
                        {"role": "inbox", "scheduler": "inbox", "watches": "human-in-the-loop"},
                        {"role": "coder", "worktree": "coder"},
                    ]
                }
            }
        }
        profile = parse_profile(config, "p")
        assert profile.inbox_watches("human-in-the-loop") is True
        assert profile.inbox_watches("coder") is False

    def test_inbox_watches_is_false_with_no_inbox_pane(self):
        assert parse_profile(CONFIG, "compact").inbox_watches("specifier") is False


class TestFileLoading:
    def test_finds_the_project_config(self, project):
        assert find_profiles_config(project) == project / "kiln" / "profiles.json"

    def test_project_override_wins_over_kiln_dir(self, project):
        override = project / "kiln.profiles.json"
        override.write_text(json.dumps(CONFIG), encoding="utf-8")
        assert find_profiles_config(project) == override

    def test_falls_back_to_the_framework_config(self, tmp_path):
        framework = tmp_path / "fw"
        (framework / "kiln" / "framework").mkdir(parents=True)
        target = framework / "kiln" / "framework" / "profiles.json"
        target.write_text(json.dumps(CONFIG), encoding="utf-8")
        assert find_profiles_config(tmp_path / "empty", framework) == target

    def test_missing_config_lists_where_it_looked(self, tmp_path):
        with pytest.raises(ProfileError, match="Searched:"):
            find_profiles_config(tmp_path / "nothing-here")


class TestSearchPaths:
    """
    The cascade the README documents, in order.

    The two shell originals disagreed about the system-wide location — PowerShell used
    ProgramData, the shell script /etc — and the first Python port kept only the Windows
    one, so `/etc/kiln/profiles.json` silently stopped working on Unix.
    """

    def _paths(self, tmp_path):
        from launcher.config import _search_paths

        return [str(p).replace("\\", "/") for p in _search_paths(tmp_path, tmp_path / "fw")]

    def test_every_documented_location_is_searched(self, tmp_path):
        found = self._paths(tmp_path)
        for expected in (
            "kiln.profiles.json",
            "kiln/profiles.json",
            ".kiln/profiles.json",
            "kiln/framework/profiles.json",
            ".kiln/profiles.json",
        ):
            assert any(path.endswith(expected) for path in found), f"{expected} not searched"

    def test_project_override_is_searched_before_the_framework(self, tmp_path):
        found = self._paths(tmp_path)
        override = next(i for i, p in enumerate(found) if p.endswith("kiln.profiles.json"))
        framework = next(i for i, p in enumerate(found) if "kiln/framework/" in p)
        assert override < framework

    def test_the_system_path_matches_the_platform(self, tmp_path):
        from launcher.config import SYSTEM_PROFILES_PATH

        expected = (
            "C:/ProgramData/kiln/profiles.json" if os.name == "nt"
            else "/etc/kiln/profiles.json"
        )
        assert str(SYSTEM_PROFILES_PATH).replace("\\", "/") == expected
        assert self._paths(tmp_path)[-1] == expected, "system-wide config must be last resort"

    def test_load_uses_the_declared_default(self, project):
        assert load_profile(project).name == "compact"

    def test_load_honours_an_explicit_name(self, project):
        assert load_profile(project, name="solo").name == "solo"

    def test_default_profile_name(self, project):
        assert default_profile_name(project) == "compact"

    def test_default_profile_name_falls_back_when_absent(self, tmp_path):
        assert default_profile_name(tmp_path / "nothing") == "default"

    def test_malformed_json_is_reported_clearly(self, tmp_path):
        root = tmp_path / "broken"
        (root / "kiln").mkdir(parents=True)
        (root / "kiln" / "profiles.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ProfileError, match="could not read"):
            load_profile(root)

    def test_list_profiles(self, project):
        assert dict(list_profiles(project))["compact"] == "Compact layout"


class TestShippedProfiles:
    def test_every_shipped_profile_parses(self):
        """Guards the real profiles.json against drifting out of the parser's grammar."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        config = json.loads(
            (repo_root / "kiln" / "framework" / "profiles.json").read_text(encoding="utf-8")
        )
        for name in config["profiles"]:
            profile = parse_profile(config, name)
            assert isinstance(profile, Profile)
            assert profile.roles, f"profile {name} has no roles"

    def test_declared_default_profile_exists(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        config = json.loads(
            (repo_root / "kiln" / "framework" / "profiles.json").read_text(encoding="utf-8")
        )
        assert config["default"] in config["profiles"]
