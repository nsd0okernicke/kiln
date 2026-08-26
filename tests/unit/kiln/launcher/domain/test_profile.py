"""
Profile parsing. A bad profile must fail loudly at launch — a role silently dropped or
routed to the wrong agent is far more expensive to diagnose once panes are running.
"""

from __future__ import annotations

import json
import os

import pytest

from kiln.launcher.domain import profile as config
from kiln.launcher.domain.profile import (
    Profile,
    ProfileError,
    RoleConfig,
    apply_agent_override,
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
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "scheduler": "python"}]}}}
        assert parse_profile(config, "p").roles[0].uses_scheduler is True

    @pytest.mark.parametrize("agent", ["copilot", "codex", "grok", "pi"])
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
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "workerDebug": True}]}}}
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

    def test_pi_is_accepted(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "pi"}]}}}
        assert parse_profile(config, "p").roles[0].agent == "pi"

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


class TestTerminationGuards:
    """
    Both guards default to "no ceiling", so an unconfigured profile behaves exactly as it did.
    What matters is that a *configured* one cannot be quietly meaningless.
    """

    def _role(self, **entry):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", **entry}]}}}
        return parse_profile(config, "p").roles[0]

    def test_unset_means_unbounded(self):
        role = self._role()
        assert role.max_cycles is None
        assert role.max_budget_usd is None

    def test_both_knobs_parse(self):
        role = self._role(maxCycles=6, maxBudgetUsd=12.5)
        assert role.max_cycles == 6
        assert role.max_budget_usd == 12.5

    @pytest.mark.parametrize("value", [0, -1])
    def test_a_non_positive_ceiling_is_rejected(self, value):
        # `maxCycles: 0` meaning "unlimited" would be the worst reading of an obvious typo:
        # the operator asked for the tightest possible bound and would get none.
        with pytest.raises(ProfileError, match="greater than zero"):
            self._role(maxCycles=value)

    def test_a_fractional_cycle_count_is_rejected(self):
        with pytest.raises(ProfileError, match="whole number"):
            self._role(maxCycles=2.5)

    def test_a_non_numeric_ceiling_is_rejected(self):
        with pytest.raises(ProfileError, match="must be a number"):
            self._role(maxBudgetUsd="lots")

    @pytest.mark.parametrize("agent", ["copilot", "codex"])
    def test_a_cost_cap_on_a_backend_that_reports_no_cost_fails_the_launch(self, agent):
        # The worst kind of guard is one that appears to be enforcing. These adapters always
        # report $0.00, so the tally never moves and the cap could never fire.
        with pytest.raises(ProfileError, match="reports no cost"):
            self._role(agent=agent, maxBudgetUsd=5.0)

    @pytest.mark.parametrize("agent", ["claude", "grok"])
    def test_a_cost_cap_is_allowed_where_cost_is_reported(self, agent):
        assert self._role(agent=agent, maxBudgetUsd=5.0).max_budget_usd == 5.0

    def test_a_verify_command_parses(self):
        role = self._role(verify="pytest -q", verifyTimeout=120)
        assert role.verify == "pytest -q"
        assert role.verify_timeout == 120

    def test_no_verify_command_is_the_default(self):
        # Every role was trusted on its own word before this existed; that stays the default.
        assert self._role().verify == ""

    def test_a_non_positive_verify_timeout_is_rejected(self):
        with pytest.raises(ProfileError, match="greater than zero"):
            self._role(verify="pytest", verifyTimeout=0)

    def test_a_cycle_limit_is_allowed_on_every_backend(self):
        # Counting laps needs no cost reporting, so this one works everywhere.
        assert self._role(agent="codex", maxCycles=3).max_cycles == 3


class TestProfileKnobs:
    """
    `--poll-interval`, `--worker-timeout` and the rest existed on every module; nothing could
    reach them from a profile, so they were compiled-in defaults for every role. `maxAttempts`
    and `escalationLimit` were worse -- dataclass defaults with no CLI flag at all, despite
    being the two numbers that decide how an unattended swarm gives up.
    """

    def _role(self, **entry):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", **entry}]}}}
        return parse_profile(config, "p").roles[0]

    def test_every_knob_parses(self):
        role = self._role(
            pollInterval=10,
            workerTimeout=60,
            maxAttempts=5,
            escalationLimit=2,
            activityLimit=20,
            bell=False,
        )
        assert role.poll_interval == 10
        assert role.worker_timeout == 60
        assert role.max_attempts == 5
        assert role.escalation_limit == 2
        assert role.activity_limit == 20
        assert role.bell is False

    def test_unset_knobs_stay_none_so_the_module_default_applies(self):
        # None, not a copy of the default: two places holding one number is how they drift.
        role = self._role()
        assert role.poll_interval is None
        assert role.worker_timeout is None
        assert role.max_attempts is None
        assert role.escalation_limit is None
        assert role.bell is True

    def test_a_fractional_poll_interval_is_allowed(self):
        # Unlike cycle counts -- half a second between polls is a perfectly good number.
        assert self._role(pollInterval=0.5).poll_interval == 0.5

    def test_a_non_positive_knob_is_rejected(self):
        with pytest.raises(ProfileError, match="greater than zero"):
            self._role(maxAttempts=0)


class TestProfileDefaults:
    """
    `full` set `agent` and `model` identically on five roles. Saying one thing five times is
    five chances for them to stop agreeing.
    """

    def _profile(self, defaults, terminals):
        config = {"profiles": {"p": {"defaults": defaults, "terminals": terminals}}}
        return parse_profile(config, "p")

    def test_terminals_inherit_the_defaults(self):
        profile = self._profile({"agent": "codex"}, [{"role": "coder"}, {"role": "architect"}])
        assert [r.agent for r in profile.roles] == ["codex", "codex"]

    def test_a_terminals_own_key_wins(self):
        profile = self._profile(
            {"agent": "codex"}, [{"role": "coder"}, {"role": "architect", "agent": "claude"}]
        )
        assert [r.agent for r in profile.roles] == ["codex", "claude"]

    def test_any_terminal_key_can_be_defaulted(self):
        # Not just agent/model: the same repetition applies to timeouts and guards.
        profile = self._profile({"workerTimeout": 60}, [{"role": "coder"}])
        assert profile.roles[0].worker_timeout == 60

    def test_a_default_role_is_rejected(self):
        # It would name every terminal the same thing.
        with pytest.raises(ProfileError, match="role"):
            self._profile({"role": "coder"}, [{"role": "coder"}])

    def test_an_unknown_default_key_is_rejected(self):
        with pytest.raises(ProfileError, match="nonsense"):
            self._profile({"nonsense": 1}, [{"role": "coder"}])

    def test_defaults_must_be_an_object(self):
        config = {"profiles": {"p": {"defaults": "claude", "terminals": [{"role": "c"}]}}}
        with pytest.raises(ProfileError, match="must be an object"):
            parse_profile(config, "p")

    def test_no_defaults_block_still_works(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "codex"}]}}}
        assert parse_profile(config, "p").roles[0].agent == "codex"


class TestAgentOverride:
    """
    `codex-only` was `full` with one word changed on five roles. A profile list is the menu
    users pick production work from, and an entry naming a *vendor* rather than a kind of work
    does not belong on it.
    """

    def _profile(self, **defaults):
        config = {
            "profiles": {
                "p": {
                    "defaults": {"agent": "claude", "model": "claude-sonnet-5", **defaults},
                    "terminals": [
                        {"role": "human-in-the-loop"},
                        {"role": "coder", "scheduler": "python"},
                        {"role": "inbox", "scheduler": "inbox", "watches": "human-in-the-loop"},
                    ],
                }
            }
        }
        return parse_profile(config, "p")

    def test_every_agent_bearing_role_moves(self):
        overridden = apply_agent_override(self._profile(), "codex")
        assert [r.agent for r in overridden.roles if not r.is_passive] == ["codex", "codex"]

    def test_the_incompatible_model_is_dropped(self):
        # The trap: `claude-sonnet-5` means nothing to the Codex CLI, so rewriting only
        # `agent` hands every role a model its backend rejects -- and the error blames the
        # model rather than the override that caused it. An empty model is the correct
        # configuration, which `resolve_model` already reads as "let the CLI choose".
        agents = [
            r for r in apply_agent_override(self._profile(), "codex").roles if not r.is_passive
        ]
        assert agents, "the fixture must contain roles that actually run an agent"
        assert all(r.model == "" for r in agents)
        assert all(r.worker_model == "" for r in agents)

    def test_a_replacement_model_can_be_given(self):
        overridden = apply_agent_override(self._profile(), "codex", "gpt-5-codex")
        assert overridden.role("coder").model == "gpt-5-codex"

    def test_passive_panes_are_left_alone(self):
        # They run no agent at all; rewriting one would be meaningless.
        before = self._profile().role("inbox")
        after = apply_agent_override(self._profile(), "codex").role("inbox")
        assert after == before

    def test_an_unknown_backend_is_refused(self):
        with pytest.raises(ProfileError, match="unsupported agent"):
            apply_agent_override(self._profile(), "gpt")

    def test_it_refuses_to_strand_a_cost_cap(self):
        # A cap that could never fire is worse than no cap -- the same rule `_parse_role`
        # already enforces, which the override could otherwise walk straight past.
        with pytest.raises(ProfileError, match="reports no cost"):
            apply_agent_override(self._profile(maxBudgetUsd=5.0), "codex")

    def test_everything_else_about_the_profile_survives(self):
        overridden = apply_agent_override(self._profile(), "codex")
        assert [r.role for r in overridden.roles] == ["human-in-the-loop", "coder", "inbox"]
        assert overridden.role("coder").scheduler == "python"

    def test_it_reproduces_what_codex_only_used_to_ship(self):
        # The retirement test: overriding the default profile must produce the shape the
        # deleted profile had -- every agent-bearing role on codex, with no model at all.
        import json
        from pathlib import Path

        repo = Path(__file__).resolve().parents[5]
        config = json.loads(
            (repo / "src" / "kiln" / "resources" / "profiles.json").read_text("utf-8")
        )
        overridden = apply_agent_override(parse_profile(config, config["default"]), "codex")

        for role in overridden.roles:
            if not role.is_passive:
                assert role.agent == "codex"
                assert role.model == ""


class TestUnknownKeys:
    """
    `_parse_role` read exactly ten keys and dropped the rest without a word, so
    `"maxAttempts": 5` was *accepted and ignored* -- the config appeared to work.
    """

    def test_an_unknown_terminal_key_fails_the_launch(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "nonsense": 1}]}}}
        with pytest.raises(ProfileError, match="nonsense"):
            parse_profile(config, "p")

    def test_the_error_names_the_role(self):
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "nonsense": 1}]}}}
        with pytest.raises(ProfileError, match="role 'coder'"):
            parse_profile(config, "p")

    def test_a_near_miss_suggests_the_real_key(self):
        # A bare "unknown key" on a profile that worked yesterday is a bad upgrade
        # experience; the message has to do the diagnosis.
        config = {"profiles": {"p": {"terminals": [{"role": "coder", "maxAttemps": 3}]}}}
        with pytest.raises(ProfileError, match="did you mean 'maxAttempts'"):
            parse_profile(config, "p")

    def test_an_unknown_profile_key_fails_too(self):
        config = {"profiles": {"p": {"terminls": [{"role": "coder"}]}}}
        with pytest.raises(ProfileError, match="terminls"):
            parse_profile(config, "p")

    def test_a_typoed_terminals_names_the_typo_not_the_symptom(self):
        # It used to report "defines no terminals" -- the symptom, not the cause.
        config = {"profiles": {"p": {"terminls": [{"role": "coder"}]}}}
        with pytest.raises(ProfileError, match="did you mean 'terminals'"):
            parse_profile(config, "p")

    def test_every_shipped_profile_still_loads(self):
        # The rejection is a breaking change; it must not break what ships with it.
        import json
        from pathlib import Path

        repo = Path(__file__).resolve().parents[5]
        config = json.loads(
            (repo / "src" / "kiln" / "resources" / "profiles.json").read_text("utf-8")
        )
        for name in config["profiles"]:
            assert parse_profile(config, name).roles


class TestCrossReferences:
    def test_watching_a_role_that_does_not_exist_fails(self):
        # `watched_role` falls back to the pane's own name, so a typo silently makes the
        # inbox watch its own queue -- empty forever, and indistinguishable from working.
        config = {
            "profiles": {
                "p": {
                    "terminals": [
                        {"role": "inbox", "scheduler": "inbox", "watches": "hooman"},
                        {"role": "human-in-the-loop"},
                    ]
                }
            }
        }
        with pytest.raises(ProfileError, match="watches 'hooman'"):
            parse_profile(config, "p")

    def test_watching_a_real_role_is_fine(self):
        config = {
            "profiles": {
                "p": {
                    "terminals": [
                        {"role": "inbox", "scheduler": "inbox", "watches": "human-in-the-loop"},
                        {"role": "human-in-the-loop"},
                    ]
                }
            }
        }
        assert parse_profile(config, "p").role("inbox").watched_role == "human-in-the-loop"

    def test_a_layout_referencing_an_unknown_role_fails(self):
        # The WezTerm Lua matches panes by name and skips a miss silently, so a typo here
        # produced a launch simply missing a pane, with no error anywhere.
        config = {
            "profiles": {
                "p": {
                    "terminals": [{"role": "coder"}],
                    "layout": {"tabs": [{"panes": [{"role": "codr"}]}]},
                }
            }
        }
        with pytest.raises(ProfileError, match="layout references role 'codr'"):
            parse_profile(config, "p")

    def test_a_layout_referencing_real_roles_is_fine(self):
        config = {
            "profiles": {
                "p": {
                    "terminals": [{"role": "coder"}],
                    "layout": {"tabs": [{"panes": [{"role": "coder"}]}]},
                }
            }
        }
        assert parse_profile(config, "p").layout["tabs"]


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
        target = framework / "src" / "kiln" / "resources" / "profiles.json"
        target.parent.mkdir(parents=True)
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
        from kiln.launcher.domain.profile import _search_paths

        return [str(p).replace("\\", "/") for p in _search_paths(tmp_path, tmp_path / "fw")]

    def test_every_documented_location_is_searched(self, tmp_path):
        found = self._paths(tmp_path)
        for expected in (
            "kiln.profiles.json",
            "kiln/profiles.json",
            ".kiln/profiles.json",
            "src/kiln/resources/profiles.json",
            ".kiln/profiles.json",
        ):
            assert any(path.endswith(expected) for path in found), f"{expected} not searched"

    def test_project_override_is_searched_before_the_framework(self, tmp_path):
        found = self._paths(tmp_path)
        override = next(i for i, p in enumerate(found) if p.endswith("kiln.profiles.json"))
        framework = next(i for i, p in enumerate(found) if "src/" in p)
        assert override < framework

    def test_the_system_path_matches_the_platform(self, tmp_path):
        from kiln.launcher.domain.profile import SYSTEM_PROFILES_PATH

        expected = (
            "C:/ProgramData/kiln/profiles.json" if os.name == "nt" else "/etc/kiln/profiles.json"
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

        repo_root = Path(__file__).resolve().parents[5]
        config = json.loads(
            (repo_root / "src" / "kiln" / "resources" / "profiles.json").read_text(encoding="utf-8")
        )
        for name in config["profiles"]:
            profile = parse_profile(config, name)
            assert isinstance(profile, Profile)
            assert profile.roles, f"profile {name} has no roles"

    def test_declared_default_profile_exists(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[5]
        config = json.loads(
            (repo_root / "src" / "kiln" / "resources" / "profiles.json").read_text(encoding="utf-8")
        )
        assert config["default"] in config["profiles"]


def _profile_with_routing(routing, terminals=None):
    return {
        "profiles": {
            "p": {
                "terminals": terminals
                or [{"role": "human-in-the-loop"}, {"role": "coder"}, {"role": "architect"}],
                "routing": routing,
            }
        }
    }


class TestProfileRouting:
    def test_parsing_a_profile_without_routing_yields_an_empty_table(self):
        # Parsing stays permissive; a *launch* is where routing has to exist.
        assert parse_profile(CONFIG, "compact").routing.rules == ()

    def test_launching_a_profile_without_routing_is_refused(self):
        from kiln.launcher.domain.profile import check_launchable

        with pytest.raises(ProfileError, match="routing"):
            check_launchable(parse_profile(CONFIG, "compact"))

    def test_a_passive_only_profile_needs_no_routing(self):
        from kiln.launcher.domain.profile import check_launchable

        passive = {
            "profiles": {
                "p": {
                    "terminals": [
                        {"role": "dashboard", "scheduler": "dashboard", "worktree": "@current"},
                    ]
                }
            }
        }
        check_launchable(parse_profile(passive, "p"))  # must not raise

    def test_every_shipped_profile_is_launchable(self):
        from pathlib import Path

        from kiln.launcher.domain.profile import check_launchable

        repo_root = Path(__file__).resolve().parents[5]
        config = json.loads(
            (repo_root / "src" / "kiln" / "resources" / "profiles.json").read_text(encoding="utf-8")
        )
        for name in config["profiles"]:
            check_launchable(parse_profile(config, name))

    def test_declared_routing_is_parsed(self):
        profile = parse_profile(_profile_with_routing({"coder": "architect"}), "p")
        assert profile.routing.resolve("coder") == "architect"

    def test_a_target_outside_the_profile_is_rejected(self):
        # A route to a role this profile never launches inserts a handoff into a queue
        # nobody polls: the cycle reports success and the work stops dead, with no error.
        with pytest.raises(ProfileError, match="specifier"):
            parse_profile(_profile_with_routing({"coder": "specifier"}), "p")

    def test_a_source_role_outside_the_profile_is_rejected(self):
        with pytest.raises(ProfileError, match="refactorer"):
            parse_profile(_profile_with_routing({"refactorer": "architect"}), "p")

    def test_a_sender_condition_outside_the_profile_is_rejected(self):
        with pytest.raises(ProfileError, match="specifier"):
            parse_profile(_profile_with_routing({"coder": {"specifier": "architect"}}), "p")

    def test_the_error_names_the_roles_that_do_exist(self):
        with pytest.raises(ProfileError, match="coder"):
            parse_profile(_profile_with_routing({"coder": "nope"}), "p")

    def test_a_malformed_routing_block_fails_the_profile_not_the_parser(self):
        with pytest.raises(ProfileError):
            parse_profile(_profile_with_routing("not-an-object"), "p")


class TestWorkflowShapedProfiles:
    """
    The profiles users actually pick from. They are named for the KIND OF WORK they do,
    which is the change: before, all three shipped profiles were the same seven roles
    differing only in which vendor ran them.
    """

    @pytest.fixture
    def shipped(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[5]
        path = repo_root / "src" / "kiln" / "resources" / "profiles.json"
        return json.loads(path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("name", ["full", "fix", "spike", "harden", "dry-run"])
    def test_each_workflow_profile_parses(self, shipped, name):
        assert parse_profile(shipped, name).roles

    def test_the_default_is_the_full_workflow(self, shipped):
        assert shipped["default"] == "full"

    @pytest.mark.parametrize("name", ["full", "fix", "spike", "harden", "dry-run"])
    def test_workflow_profiles_use_pi_with_role_specific_models(self, shipped, name):
        profile = parse_profile(shipped, name)
        active_roles = [role for role in profile.roles if not role.is_passive]

        assert {role.agent for role in active_roles} == {"pi"}
        assert profile.role("human-in-the-loop").model == "igate/brain"
        assert {
            role.model for role in active_roles if role.role != "human-in-the-loop"
        } == {"igate/coder"}

    def test_every_profile_has_a_human_entry_point(self, shipped):
        for name in shipped["profiles"]:
            roles = {r.role for r in parse_profile(shipped, name).roles}
            assert "human-in-the-loop" in roles, name

    def test_harden_needs_no_specifier(self, shipped):
        # The shape that forced routing into the profile: no specifier means the architect
        # cannot hand back to one.
        roles = {r.role for r in parse_profile(shipped, "harden").roles}
        assert "specifier" not in roles

    def test_the_architect_row_that_could_not_coexist(self, shipped):
        # In one shared table these are the same (role, when_sender) key, and the second is
        # a hard parse failure that takes down every profile -- not a quiet misroute.
        assert parse_profile(shipped, "full").routing.resolve("architect") == "specifier"
        assert parse_profile(shipped, "harden").routing.resolve("architect") == (
            "human-in-the-loop"
        )

    def test_every_profile_declares_its_own_routing(self, shipped):
        # No inheritance and no fallback: the profile is the only place routing lives.
        for name in shipped["profiles"]:
            assert parse_profile(shipped, name).routing.rules, name

    def test_the_full_cycle_still_closes_back_to_the_human(self, shipped):
        # The conditional row that stops an architect's completed-cycle report looping
        # round to the coder forever.
        routing = parse_profile(shipped, "full").routing
        assert routing.resolve("specifier") == "coder"
        assert routing.resolve("specifier", "architect") == "human-in-the-loop"

    @pytest.mark.parametrize("name", ["fix", "spike", "harden"])
    def test_a_reshaped_profile_routes_back_to_the_human(self, shipped, name):
        # Every shortened cycle must terminate somewhere a human is looking, or the run
        # simply stops with nothing to read.
        profile = parse_profile(shipped, name)
        targets = {rule.target for rule in profile.routing.rules}
        assert "human-in-the-loop" in targets

    def test_dry_run_puts_every_agent_role_in_manual_mode(self, shipped):
        for role in parse_profile(shipped, "dry-run").roles:
            if not role.is_passive:
                assert role.mode == "manual", role.role
