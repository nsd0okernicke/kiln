"""
Workspace setup against real git repositories.

Most assertions here encode a failure that has actually happened: a tracked `.kiln` symlink
breaking later merges, a stray `kiln/.gitignore` hiding the whole constitution from every
worktree, a BOM making git refuse to run the pre-push hook.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kiln.launcher.domain.paths import KilnPaths
from kiln.launcher.domain.profile import parse_profile
from kiln.launcher.infrastructure import workspace
from kiln.scheduler.infrastructure.vcs import git as git_ops

pytestmark = pytest.mark.integration

PROFILE = parse_profile(
    {
        "profiles": {
            "p": {
                "terminals": [
                    {"role": "specifier", "worktree": "@current", "mode": "manual"},
                    {"role": "coder", "worktree": "coder", "mode": "auto"},
                ]
            }
        }
    },
    "p",
)


@pytest.fixture
def paths(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    framework = tmp_path / "fw"
    claude_resources = framework / "src" / "kiln" / "resources" / "claude"
    claude_resources.mkdir(parents=True, exist_ok=True)
    (claude_resources / "settings.json").write_text("{}", encoding="utf-8")
    (framework / "src" / "kiln" / "resources" / "tools").mkdir(parents=True)
    (framework / "src" / "kiln" / "resources" / "tools" / "set-status.py").write_text(
        "print('status')\n", encoding="utf-8"
    )
    return KilnPaths.create(project, framework)


@pytest.fixture
def repo(paths):
    workspace.run_git(["init", "-b", "main"], paths.project_root, check=True)
    workspace.run_git(["config", "user.email", "t@example.com"], paths.project_root)
    workspace.run_git(["config", "user.name", "Test"], paths.project_root)
    (paths.project_root / "README.md").write_text("x\n", encoding="utf-8")
    workspace.run_git(["add", "-A"], paths.project_root)
    workspace.run_git(["commit", "-qm", "initial"], paths.project_root)
    return paths


class TestGitignore:
    def test_creates_with_all_required_entries(self, paths):
        content = workspace.ensure_gitignore(paths).read_text(encoding="utf-8")
        for entry in workspace.REQUIRED_GITIGNORE_ENTRIES:
            assert entry in content.splitlines()

    def test_kiln_entry_has_no_trailing_slash(self):
        # A trailing slash only matches real directories, not the .kiln symlink — with it,
        # the symlink stays untracked-but-not-ignored and can be committed by accident.
        assert ".kiln" in workspace.REQUIRED_GITIGNORE_ENTRIES
        assert ".kiln/" not in workspace.REQUIRED_GITIGNORE_ENTRIES

    def test_tops_up_an_existing_file(self, paths):
        target = paths.project_root / ".gitignore"
        target.write_text("node_modules/\n.kiln\n", encoding="utf-8")
        content = workspace.ensure_gitignore(paths).read_text(encoding="utf-8")
        assert "node_modules/" in content, "existing entries must be preserved"
        assert "tmp/" in content
        assert content.count(".kiln\n") == 1, "must not duplicate an existing entry"

    def test_is_idempotent(self, paths):
        workspace.ensure_gitignore(paths)
        first = (paths.project_root / ".gitignore").read_text(encoding="utf-8")
        workspace.ensure_gitignore(paths)
        assert (paths.project_root / ".gitignore").read_text(encoding="utf-8") == first


class TestRepoInit:
    def test_initializes_a_fresh_project(self, paths):
        workspace.initialize_repo(paths)
        assert (paths.project_root / ".git").exists()
        assert workspace.run_git(["rev-parse", "HEAD"], paths.project_root).returncode == 0

    def test_commits_gitignore_in_an_existing_repo(self, repo):
        # Must be tracked before worktrees exist, or each worktree starts without it.
        workspace.initialize_repo(repo)
        tracked = workspace.run_git(
            ["ls-files", "--error-unmatch", ".gitignore"], repo.project_root
        )
        assert tracked.returncode == 0

    def test_current_branch(self, repo):
        assert workspace.current_branch(repo) == "main"

    def test_current_branch_falls_back_outside_a_repo(self, paths):
        assert workspace.current_branch(paths) == "kiln"


class TestGitHooks:
    def test_writes_the_pre_push_hook(self, repo):
        hook = workspace.install_git_hooks(repo)
        assert hook.exists()
        assert "kiln-sub-branches" in hook.read_text(encoding="utf-8")

    def test_hook_has_no_bom_and_lf_endings(self, repo):
        # A BOM breaks git's shebang detection: "cannot spawn .git/hooks/pre-push".
        raw = workspace.install_git_hooks(repo).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in raw
        assert raw.startswith(b"#!/bin/sh")

    def test_does_not_overwrite_an_existing_hook(self, repo):
        hook = repo.project_root / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("# mine\n", encoding="utf-8")
        workspace.install_git_hooks(repo)
        assert hook.read_text(encoding="utf-8") == "# mine\n"


class TestStateDirs:
    def test_creates_ephemeral_directories_with_self_ignoring_markers(self, paths):
        workspace.prepare_state_dirs(paths)
        assert (paths.state_dir / ".gitignore").read_text(encoding="utf-8").strip() == "*"
        assert (paths.worktrees_dir / ".gitignore").exists()
        assert paths.logs_dir.is_dir() and paths.status_dir.is_dir()

    def test_removes_a_stray_kiln_gitignore(self, paths):
        # This file silently excluded kiln/project/ from every commit, leaving worktrees
        # with no constitution at all.
        paths.kiln_dir.mkdir(parents=True, exist_ok=True)
        (paths.kiln_dir / ".gitignore").write_text("*\n", encoding="utf-8")
        workspace.prepare_state_dirs(paths)
        assert not (paths.kiln_dir / ".gitignore").exists()

    def test_keeps_a_legitimate_kiln_gitignore(self, paths):
        paths.kiln_dir.mkdir(parents=True, exist_ok=True)
        (paths.kiln_dir / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        workspace.prepare_state_dirs(paths)
        assert (paths.kiln_dir / ".gitignore").exists()

    def test_copies_framework_tools(self, paths):
        workspace.prepare_state_dirs(paths)
        workspace.copy_framework_tools(paths)
        assert (paths.state_tools_dir / "set-status.py").is_file()


class TestWorktrees:
    def test_creates_one_per_non_current_role(self, repo):
        workspace.prepare_state_dirs(repo)
        created = workspace.prepare_worktrees(PROFILE, repo, "main")
        assert len(created) == 1, "the @current role must not get a worktree"
        assert (repo.worktrees_dir / "coder").is_dir()

    def test_uses_a_role_sub_branch(self, repo):
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        branch = workspace.run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], repo.worktrees_dir / "coder"
        )
        assert branch.stdout.strip() == "main-coder"

    def test_records_sub_branches_for_the_push_guard(self, repo):
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        listed = (repo.project_root / ".git" / "kiln-sub-branches").read_text(encoding="utf-8")
        assert "main-coder" in listed

    def test_links_shared_state_into_the_worktree(self, repo):
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        assert (repo.worktrees_dir / "coder" / ".kiln").exists()

    def test_writes_a_role_scoped_mcp_config(self, repo):
        import json

        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        config = json.loads(
            (repo.worktrees_dir / "coder" / ".mcp.json").read_text(encoding="utf-8")
        )
        assert config["mcpServers"]["kiln-channel"]["env"]["KILN_ROLE"] == "coder"

    def test_creates_the_tmp_directory(self, repo):
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        assert (repo.worktrees_dir / "coder" / "tmp").is_dir()

    def test_is_idempotent(self, repo):
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        workspace.prepare_worktrees(PROFILE, repo, "main")
        assert (repo.worktrees_dir / "coder").is_dir()


class TestWarnIfWorktreeConflicts:
    def test_no_warning_when_branch_is_just_behind(self, repo, caplog):
        # role_branch hasn't caught up to main yet -- normal mid-run state, not a stale worktree
        workspace.run_git(["branch", "role"], repo.project_root)
        (repo.project_root / "README.md").write_text("y\n", encoding="utf-8")
        workspace.run_git(["add", "-A"], repo.project_root)
        workspace.run_git(["commit", "-qm", "advance main"], repo.project_root)

        with caplog.at_level("WARNING"):
            warned = workspace.warn_if_worktree_conflicts(repo, "role", "main")

        assert warned is False
        assert not caplog.records

    def test_warns_on_a_genuine_content_conflict(self, repo, caplog):
        # both branches independently add the same file with different content -- the
        # signature of a worktree left over from an earlier, unrelated run (see logbook.md
        # add/add conflict observed live in library-hub-testrun4)
        workspace.run_git(["checkout", "-b", "role"], repo.project_root)
        (repo.project_root / "logbook.md").write_text("role side\n", encoding="utf-8")
        workspace.run_git(["add", "-A"], repo.project_root)
        workspace.run_git(["commit", "-qm", "role logbook"], repo.project_root)
        workspace.run_git(["checkout", "main"], repo.project_root)
        (repo.project_root / "logbook.md").write_text("main side\n", encoding="utf-8")
        workspace.run_git(["add", "-A"], repo.project_root)
        workspace.run_git(["commit", "-qm", "main logbook"], repo.project_root)

        with caplog.at_level("WARNING"):
            warned = workspace.warn_if_worktree_conflicts(repo, "role", "main")

        assert warned is True
        assert any("CONFLICT" in record.message for record in caplog.records)


class TestSkills:
    def _add_skill(self, paths, name="mutation-testing"):
        skill = paths.skills_dir / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

    def test_no_skills_directory_is_not_an_error(self, paths):
        assert workspace.prepare_skills(PROFILE, paths) == 0

    def test_empty_skills_directory_is_not_an_error(self, paths):
        paths.skills_dir.mkdir(parents=True)
        assert workspace.prepare_skills(PROFILE, paths) == 0

    def test_native_symlink_path_counts_each_created_link(self, repo, monkeypatch):
        self._add_skill(repo)
        linked = []
        monkeypatch.setattr(
            Path,
            "symlink_to",
            lambda target, source, target_is_directory: linked.append(
                (target, source, target_is_directory)
            ),
        )

        count = workspace.prepare_skills(PROFILE, repo)

        assert count == len(linked)
        assert count > 0
        assert all(is_directory for _, _, is_directory in linked)

    def test_links_skills_for_every_role(self, repo):
        self._add_skill(repo)
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        assert workspace.prepare_skills(PROFILE, repo) > 0
        assert (repo.project_root / ".claude" / "skills" / "mutation-testing").exists()

    def test_every_skill_convention_is_populated_in_every_worktree(self, repo):
        # Copilot's own `skill --help` documents that it checks .github/skills, .agents/skills,
        # AND .claude/skills from cwd -- not just the one usually thought of as "its own" -- so
        # a role's worktree must keep all three in sync, not only the location matching its
        # current agent.
        self._add_skill(repo)
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")
        workspace.prepare_skills(PROFILE, repo)
        coder_root = repo.worktree_path("coder")
        for parent, name in workspace.SKILL_DIR_CONVENTIONS:
            assert (coder_root / parent / name / "mutation-testing").exists()

    def test_removed_skills_do_not_linger(self, repo):
        self._add_skill(repo, "old-skill")
        workspace.prepare_state_dirs(repo)
        workspace.prepare_skills(PROFILE, repo)
        import shutil

        shutil.rmtree(repo.skills_dir / "old-skill")
        self._add_skill(repo, "new-skill")
        workspace.prepare_skills(PROFILE, repo)
        skills_root = repo.project_root / ".claude" / "skills"
        assert (skills_root / "new-skill").exists()
        assert not (skills_root / "old-skill").exists()

    def test_scheduler_mode_roles_skip_wrapper_only_meta_skills(self, repo):
        # A one-shot scheduler worker has been observed following kiln-receive's
        # message-queue protocol against MCP access the scheduler adapter deliberately
        # disables, turning an expected permission denial into a confused, credit-burning
        # session that never produces a result. The scheduler handles receive/merge/handoff
        # mechanically in Python, so the worker never needs these skills.
        self._add_skill(repo, "kiln-receive")
        self._add_skill(repo, "kiln-handoff")
        self._add_skill(repo, "kiln-ping")
        self._add_skill(repo, "tdd-red")
        profile = parse_profile(
            {
                "profiles": {
                    "p": {
                        "terminals": [
                            {"role": "specifier", "worktree": "@current", "mode": "manual"},
                            {
                                "role": "coder",
                                "agent": "copilot",
                                "worktree": "coder",
                                "mode": "auto",
                                "scheduler": "python",
                            },
                        ]
                    }
                }
            },
            "p",
        )
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(profile, repo, "main")
        workspace.prepare_skills(profile, repo)

        wrapper_skills = repo.project_root / ".claude" / "skills"
        assert (wrapper_skills / "kiln-receive").exists()
        assert (wrapper_skills / "tdd-red").exists()

        # Copilot scans .github/skills, .agents/skills, AND .claude/skills from cwd (confirmed
        # via `copilot skill --help`) -- every convention in the worker's worktree must be
        # filtered, not just the one nominally "belonging" to its agent.
        coder_root = repo.worktree_path("coder")
        for parent, name in workspace.SKILL_DIR_CONVENTIONS:
            worker_skills = coder_root / parent / name
            assert not (worker_skills / "kiln-receive").exists()
            assert not (worker_skills / "kiln-handoff").exists()
            assert not (worker_skills / "kiln-ping").exists()
            assert (worker_skills / "tdd-red").exists()

    def test_a_stale_directory_from_a_former_agent_does_not_leak_wrapper_skills(self, repo):
        # Observed live: a role that used to run as `agent: claude` (or was never cleaned up
        # after a profile change) leaves .claude/skills populated with the full, unfiltered
        # skill set. When the role becomes a scheduler-mode `agent: copilot` role, the old code
        # only ever rewrote .github/skills -- the directory matching the *current* agent -- and
        # left .claude/skills untouched, which Copilot still reads from the same cwd. The
        # worker followed kiln-receive's protocol anyway despite it being excluded from
        # .github/skills.
        self._add_skill(repo, "kiln-receive")
        self._add_skill(repo, "tdd-red")
        profile = parse_profile(
            {
                "profiles": {
                    "p": {
                        "terminals": [
                            {"role": "specifier", "worktree": "@current", "mode": "manual"},
                            {
                                "role": "coder",
                                "agent": "copilot",
                                "worktree": "coder",
                                "mode": "auto",
                                "scheduler": "python",
                            },
                        ]
                    }
                }
            },
            "p",
        )
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(profile, repo, "main")

        stale = repo.worktree_path("coder") / ".claude" / "skills" / "kiln-receive"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

        workspace.prepare_skills(profile, repo)

        assert not stale.exists()


GROK_PROFILE = parse_profile(
    {
        "profiles": {
            "p": {
                "terminals": [
                    {"role": "specifier", "worktree": "@current", "mode": "manual"},
                    {
                        "role": "coder",
                        "agent": "grok",
                        "worktree": "coder",
                        "mode": "auto",
                    },
                ]
            }
        }
    },
    "p",
)


class TestGrokWorktreeSetup:
    """
    What a grok wrapper role needs in its worktree, all verified with `grok inspect` against
    grok 1.0.5: its worker definition under `.grok/agents/`, skills in a convention it scans,
    and a `.mcp.json` carrying `kiln-db` but *not* `kiln-channel`.

    Grok needs no per-backend MCP config of its own. It reads the worktree `.mcp.json`
    directly, exactly as Claude does -- which also means the channel entry is not inert for
    it the way it is for Codex and Copilot, and has to be withheld to match its polling loop.
    """

    def test_the_worker_definition_is_mirrored_into_the_worktree(self, repo):
        # Written to the project root by generate.write_worker_file, then copied per worktree
        # -- a grok wrapper discovers `<role>-worker` as a project agent from its own cwd.
        source = repo.project_root / ".grok" / "agents"
        source.mkdir(parents=True)
        (source / "coder-worker.md").write_text("---\nname: coder-worker\n---\n", encoding="utf-8")

        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(GROK_PROFILE, repo, "main")

        assert (repo.worktree_path("coder") / ".grok" / "agents" / "coder-worker.md").is_file()

    def test_the_worktree_mcp_config_carries_kiln_db(self, repo):
        # Without it the pane launches and can then do nothing: no query to poll with, no
        # insert to hand off with.
        import json

        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(GROK_PROFILE, repo, "main")

        config = json.loads((repo.worktree_path("coder") / ".mcp.json").read_text(encoding="utf-8"))
        assert "kiln-db" in config["mcpServers"]

    def test_the_worktree_mcp_config_withholds_the_blocking_channel(self, repo):
        # Grok's loop polls kiln-db and is told never to call wait_for_message(). Since grok
        # really does read this file, listing the channel would advertise a tool the prose
        # forbids -- the same contradiction scheduler roles are already protected from.
        import json

        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(GROK_PROFILE, repo, "main")

        config = json.loads((repo.worktree_path("coder") / ".mcp.json").read_text(encoding="utf-8"))
        assert "kiln-channel" not in config["mcpServers"]

    def test_a_claude_wrapper_role_still_gets_the_channel(self, repo):
        # The blocking loop is Claude's and must not be collateral damage of the above.
        import json

        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(PROFILE, repo, "main")

        config = json.loads((repo.worktree_path("coder") / ".mcp.json").read_text(encoding="utf-8"))
        assert "kiln-channel" in config["mcpServers"]

    def test_grok_needs_no_backend_config_of_its_own(self, repo):
        # It reads .mcp.json, so there is deliberately nothing for prepare_agent_configs to
        # write -- an earlier version of this wrote a redundant .grok/config.toml.
        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(GROK_PROFILE, repo, "main")
        workspace.prepare_agent_configs(GROK_PROFILE, repo)

        assert not (repo.worktree_path("coder") / ".grok" / "config.toml").exists()

    def test_skills_are_linked_for_a_grok_role(self, repo):
        # Verified live: grok scans .claude/skills and .agents/skills (and .grok/skills), but
        # NOT .github/skills. The two it shares with Kiln's existing conventions are enough,
        # so grok needs no new convention -- only to stop being filtered out of the loop.
        skill = repo.skills_dir / "tdd-red"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")

        workspace.prepare_state_dirs(repo)
        workspace.prepare_worktrees(GROK_PROFILE, repo, "main")
        workspace.prepare_skills(GROK_PROFILE, repo)

        coder_root = repo.worktree_path("coder")
        assert (coder_root / ".claude" / "skills" / "tdd-red").exists()
        assert (coder_root / ".agents" / "skills" / "tdd-red").exists()


class TestLogbookUnionMerge:
    """
    Found live: a swarm wedged on `CONFLICT (add/add): Merge conflict in logbook.md`.

    `/kiln-receive`, `/kiln-handoff` and `/kiln-ping` all tell the agent to append a line to
    `logbook.md` and commit it -- in its own worktree, on its own branch. Two branches adding
    different lines to the end of one tracked file is the classic changelog conflict, and it
    fires every cycle regardless of what the swarm is building. Worse, the squash mechanics
    leave the merge base with no `logbook.md` at all, so git reports `add/add` and refuses to
    merge the contents rather than doing a resolvable three-way merge.
    """

    def test_the_local_attributes_file_declares_it(self, git_repo):
        git_ops.ensure_union_merge(git_repo)
        attributes = Path(
            git_ops.run_git(["rev-parse", "--git-path", "info/attributes"], git_repo).stdout
        )
        if not attributes.is_absolute():
            attributes = git_repo / attributes
        assert "logbook.md merge=union" in attributes.read_text(encoding="utf-8")

    def test_it_is_idempotent(self, git_repo):
        for _ in range(3):
            git_ops.ensure_union_merge(git_repo)
        attributes = Path(
            git_ops.run_git(["rev-parse", "--git-path", "info/attributes"], git_repo).stdout
        )
        if not attributes.is_absolute():
            attributes = git_repo / attributes
        text = attributes.read_text(encoding="utf-8")
        assert text.count("logbook.md merge=union") == 1

    def test_it_actually_resolves_an_add_add_conflict(self, git_repo, git_cmd):
        # The behaviour, not the file: an attributes entry that did not change the merge
        # outcome would be worthless, and `add/add` is the case a plain textual merge driver
        # would still refuse.
        git_ops.ensure_union_merge(git_repo)
        git_cmd(git_repo, "checkout", "-q", "-b", "role-a")
        (git_repo / "logbook.md").write_text("[SENT] from a\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "a")

        git_cmd(git_repo, "checkout", "-q", "main")
        git_cmd(git_repo, "checkout", "-q", "-b", "role-b")
        (git_repo / "logbook.md").write_text("[RECEIVED] from b\n", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-qm", "b")

        merged = git_ops.run_git(["merge", "role-a", "-m", "merge"], git_repo)

        assert merged.ok, f"merge should not conflict: {merged.output}"
        logbook = (git_repo / "logbook.md").read_text(encoding="utf-8")
        assert "from a" in logbook and "from b" in logbook, "both sides' lines must survive"

    def test_a_committed_gitattributes_carries_the_rule_to_a_clone(self, paths):
        # The local file is invisible to a fresh clone or to a human merging by hand.
        workspace.ensure_gitattributes(paths)
        written = (paths.project_root / ".gitattributes").read_text(encoding="utf-8")
        assert "logbook.md merge=union" in written

    def test_it_tops_up_an_existing_gitattributes_without_clobbering_it(self, paths):
        path = paths.project_root / ".gitattributes"
        path.write_text("*.png binary\n", encoding="utf-8")

        workspace.ensure_gitattributes(paths)

        written = path.read_text(encoding="utf-8")
        assert "*.png binary" in written, "the project's own rules must survive"
        assert "logbook.md merge=union" in written

    def test_topping_up_is_idempotent(self, paths):
        for _ in range(3):
            workspace.ensure_gitattributes(paths)
        written = (paths.project_root / ".gitattributes").read_text(encoding="utf-8")
        assert written.count("logbook.md merge=union") == 1


class TestAgentConfigs:
    def test_codex_role_gets_an_isolated_home_with_trust(self, paths):
        profile = parse_profile(
            {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "codex"}]}}}, "p"
        )
        workspace.prepare_agent_configs(profile, paths)
        config = (paths.codex_home("coder") / "config.toml").read_text(encoding="utf-8")
        assert 'trust_level = "trusted"' in config
        assert "kiln-db" in config

    def test_no_codex_home_without_a_codex_role(self, paths):
        workspace.prepare_agent_configs(PROFILE, paths)
        assert not paths.codex_home("coder").exists()

    def test_the_mcp_server_gets_a_generous_startup_timeout(self, paths):
        # Codex's own default is too short for `npx mcp-sqlite`, which resolves (and on a
        # cold cache downloads) the package before the server says anything. Observed live:
        # "MCP client for `kiln-db` timed out". A role without kiln-db cannot hand off at all.
        profile = parse_profile(
            {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "codex"}]}}}, "p"
        )
        workspace.prepare_agent_configs(profile, paths)
        config = (paths.codex_home("coder") / "config.toml").read_text(encoding="utf-8")
        assert "startup_timeout_sec" in config


class TestCodexAuthSeeding:
    """
    Found live: a wrapper-mode Codex role sent an unauthenticated request and the upstream
    answered `401 Unauthorized`. The isolated CODEX_HOME exists to protect the user's real
    `config.toml` from Kiln's per-role trust and MCP entries -- it was never meant to isolate
    their *identity*, but with no `auth.json` in it that is exactly what it did. Every shipped
    profile had `human-in-the-loop` on Claude, so this path had never run.
    """

    def _fake_real_home(self, tmp_path, monkeypatch, credentials="{}"):
        home = tmp_path / "real-codex"
        home.mkdir()
        if credentials is not None:
            (home / "auth.json").write_text(credentials, encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(home))
        return home

    def test_credentials_are_copied_into_the_roles_home(self, tmp_path, monkeypatch):
        self._fake_real_home(tmp_path, monkeypatch, '{"token": "abc"}')
        role_home = tmp_path / "role-home"
        role_home.mkdir()

        assert workspace.seed_codex_auth(role_home) is True
        assert (role_home / "auth.json").read_text(encoding="utf-8") == '{"token": "abc"}'

    def test_a_launch_seeds_every_codex_role(self, paths, tmp_path, monkeypatch):
        self._fake_real_home(tmp_path, monkeypatch)
        profile = parse_profile(
            {
                "profiles": {
                    "p": {
                        "terminals": [
                            {"role": "coder", "agent": "codex"},
                            {"role": "architect", "agent": "codex"},
                        ]
                    }
                }
            },
            "p",
        )

        workspace.prepare_agent_configs(profile, paths)

        for role in ("coder", "architect"):
            assert (paths.codex_home(role) / "auth.json").is_file()

    def test_missing_credentials_warn_rather_than_fail_the_launch(
        self, tmp_path, monkeypatch, caplog
    ):
        # "Not logged in" is an ordinary state, and the CLI says so far better than a
        # traceback from the launcher would.
        self._fake_real_home(tmp_path, monkeypatch, credentials=None)
        role_home = tmp_path / "role-home"
        role_home.mkdir()

        with caplog.at_level(logging.WARNING):
            assert workspace.seed_codex_auth(role_home) is False

        assert "codex login" in caplog.text

    def test_it_reads_the_codex_home_environment_variable(self, tmp_path, monkeypatch):
        # A user who relocated their Codex config must not be told they are logged out.
        elsewhere = self._fake_real_home(tmp_path, monkeypatch)
        assert workspace.real_codex_home() == elsewhere

    def test_it_falls_back_to_the_default_location(self, monkeypatch):
        monkeypatch.delenv("CODEX_HOME", raising=False)
        assert workspace.real_codex_home().name == ".codex"


class TestTrustCopilotWorktrees:
    """
    Regression: a git worktree is a different filesystem path from the project root even
    though it shares history, so Copilot's per-path workspace-trust check does not inherit
    from a trusted root. Observed live: every write-capable tool (including the `kiln-db` MCP
    tool) returned "Permission denied and could not request permission from user" for 8+
    minutes before the worker gave up having made zero changes.
    """

    @pytest.fixture
    def fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".copilot").mkdir(parents=True)
        monkeypatch.setattr(workspace.Path, "home", lambda: home)
        return home

    def _config_path(self, home):
        return home / ".copilot" / "config.json"

    def _seed_config(self, home, trusted_folders):
        import json

        self._config_path(home).write_text(
            "// User settings belong in settings.json.\n"
            "// This file is managed automatically.\n"
            + json.dumps({"trustedFolders": trusted_folders}, indent=2),
            encoding="utf-8",
        )

    def _read_trusted_folders(self, home):
        import json

        text = self._config_path(home).read_text(encoding="utf-8")
        return json.loads(text[text.index("{") :])["trustedFolders"]

    def _profile_with_copilot(self, paths, role="coder"):
        return parse_profile(
            {
                "profiles": {
                    "p": {"terminals": [{"role": role, "agent": "copilot", "worktree": role}]}
                }
            },
            "p",
        )

    def test_adds_the_copilot_worktree_to_trusted_folders(self, paths, fake_home):
        self._seed_config(fake_home, [str(paths.project_root)])
        workspace.prepare_agent_configs(self._profile_with_copilot(paths), paths)
        assert str(paths.worktree_path("coder")) in self._read_trusted_folders(fake_home)

    def test_preserves_folders_the_user_already_trusted_interactively(self, paths, fake_home):
        self._seed_config(fake_home, [r"C:\Users\someone", str(paths.project_root)])
        workspace.prepare_agent_configs(self._profile_with_copilot(paths), paths)
        assert r"C:\Users\someone" in self._read_trusted_folders(fake_home)

    def test_preserves_the_managed_by_comment_header(self, paths, fake_home):
        self._seed_config(fake_home, [str(paths.project_root)])
        workspace.prepare_agent_configs(self._profile_with_copilot(paths), paths)
        content = self._config_path(fake_home).read_text(encoding="utf-8")
        assert content.startswith("// User settings belong in settings.json.")

    def test_a_current_dir_copilot_role_trusts_the_project_root(self, paths, fake_home):
        self._seed_config(fake_home, [])
        profile = parse_profile(
            {"profiles": {"p": {"terminals": [{"role": "coder", "agent": "copilot"}]}}}, "p"
        )
        workspace.prepare_agent_configs(profile, paths)
        assert str(paths.project_root) in self._read_trusted_folders(fake_home)

    def test_no_copilot_role_touches_nothing(self, paths, fake_home):
        self._seed_config(fake_home, [str(paths.project_root)])
        before = self._config_path(fake_home).read_text(encoding="utf-8")
        workspace.prepare_agent_configs(PROFILE, paths)
        assert self._config_path(fake_home).read_text(encoding="utf-8") == before

    def test_missing_config_file_is_a_warning_not_a_crash(
        self, paths, tmp_path, monkeypatch, caplog
    ):
        home = tmp_path / "no-copilot-yet"
        home.mkdir()
        monkeypatch.setattr(workspace.Path, "home", lambda: home)
        import logging

        with caplog.at_level(logging.WARNING):
            workspace.prepare_agent_configs(self._profile_with_copilot(paths), paths)
        assert "run `copilot` interactively" in caplog.text

    def test_malformed_config_file_is_a_warning_not_a_crash(self, paths, fake_home, caplog):
        self._config_path(fake_home).write_text("not json at all", encoding="utf-8")
        import logging

        with caplog.at_level(logging.WARNING):
            workspace.prepare_agent_configs(self._profile_with_copilot(paths), paths)
        assert "could not read" in caplog.text

    def test_already_trusted_worktree_is_left_untouched(self, paths, fake_home):
        worktree = str(paths.worktree_path("coder"))
        self._seed_config(fake_home, [str(paths.project_root), worktree])
        before = self._config_path(fake_home).read_text(encoding="utf-8")
        workspace.prepare_agent_configs(self._profile_with_copilot(paths), paths)
        assert self._config_path(fake_home).read_text(encoding="utf-8") == before


class TestSessionsFile:
    def test_lists_every_role(self, paths):
        content = workspace.write_sessions_file(PROFILE, paths).read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert len(lines) == 2
        # Trailing empty model: this fixture's roles configure none, which is a real state
        # meaning "the backend's CLI chooses", not a missing value.
        assert lines[0].split("\t") == [
            "1",
            "specifier",
            "claude",
            "Specifier",
            "agent",
            "",
            "@current",
        ]

    def test_it_records_each_panes_kind(self, paths):
        # This file is the only thing the terminal dashboard and the web cockpit read; the
        # profile is long gone by then. Without the kind neither can tell a role that
        # reports state from a pane that structurally cannot.
        profile = parse_profile(
            {
                "profiles": {
                    "p": {
                        "terminals": [
                            {"role": "human-in-the-loop", "worktree": "@current", "mode": "manual"},
                            {"role": "coder", "worktree": "coder", "scheduler": "python"},
                            {
                                "role": "inbox",
                                "worktree": "@current",
                                "mode": "manual",
                                "scheduler": "inbox",
                                "watches": "human-in-the-loop",
                            },
                            {
                                "role": "cockpit",
                                "worktree": "@current",
                                "mode": "manual",
                                "scheduler": "cockpit",
                            },
                        ]
                    }
                }
            },
            "p",
        )

        content = workspace.write_sessions_file(profile, paths).read_text(encoding="utf-8")

        kinds = [line.split("\t")[4] for line in content.strip().splitlines()]
        assert kinds == ["agent", "python", "inbox", "cockpit"]

    def test_it_records_the_profiles_model_for_wrapper_roles(self, paths):
        # A wrapper role has no scheduler, so nothing ever writes it a status model. Without
        # this column its model is permanently unknown, which is what the cockpit showed for
        # `human-in-the-loop`.
        profile = parse_profile(
            {
                "profiles": {
                    "p": {
                        "defaults": {"model": "claude-sonnet-5"},
                        "terminals": [
                            {"role": "human-in-the-loop", "worktree": "@current", "mode": "manual"},
                            {"role": "coder", "worktree": "coder", "model": ""},
                        ],
                    }
                }
            },
            "p",
        )

        content = workspace.write_sessions_file(profile, paths).read_text(encoding="utf-8")

        # Split without stripping the whole file: the last line's model column is empty here,
        # so a trailing `.strip()` would eat its tab and hide the column entirely.
        # `read_sessions` tolerates that (a short row falls back to ""), but the test must
        # assert on what was actually written.
        rows = [line.split("\t") for line in content.splitlines() if line]
        assert [row[5] for row in rows] == ["claude-sonnet-5", ""]

    def test_the_kind_column_round_trips_through_the_reader(self, paths):
        # The writer and the reader live in different packages, so the format is only
        # actually agreed if one is fed the other's output.
        from kiln.scheduler.infrastructure.cli import dashboard

        path = workspace.write_sessions_file(PROFILE, paths)

        sessions = dashboard.read_sessions(path)

        assert [s.role for s in sessions] == ["specifier", "coder"]
        assert all(s.passive is False for s in sessions)
        assert [s.worktree for s in sessions] == ["@current", "coder"]

    def test_the_launcher_can_record_full_worktree_identities(self, paths):
        from kiln.scheduler.infrastructure.cli import dashboard

        sessions = dashboard.read_sessions(
            workspace.write_sessions_file(PROFILE, paths, branch="run1")
        )

        assert [session.worktree for session in sessions] == ["run1", "run1-coder"]


class TestTemplateCopying:
    """
    Copying framework templates must not replay the source's mode or mtime.

    `shutil.copy2`/`copytree` were used, and their trailing `chmod`/`utime` calls are
    rejected with EPERM on a WSL DrvFs mount of a Windows drive whose uid mapping differs
    from the calling user. `kiln init` into a `/mnt/c/...` directory therefore died on a raw
    PermissionError traceback with the project half-created (confirmed on Ubuntu 24.04).
    Network shares and container volume mounts reject the same calls.
    """

    @pytest.fixture
    def no_metadata_calls(self, monkeypatch):
        """Make chmod/utime fail exactly the way a DrvFs mount makes them fail."""
        import os

        def deny(*_args, **_kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "chmod", deny)
        monkeypatch.setattr(os, "utime", deny)

    def test_file_copy_survives_a_filesystem_refusing_metadata(self, tmp_path, no_metadata_calls):
        source = tmp_path / "engineering.md"
        source.write_text("rules\n", encoding="utf-8")
        destination = tmp_path / "out" / "engineering.md"
        destination.parent.mkdir()

        workspace.copy_template_file(source, destination)
        assert destination.read_text(encoding="utf-8") == "rules\n"

    def test_tree_copy_survives_a_filesystem_refusing_metadata(self, tmp_path, no_metadata_calls):
        # shutil.copytree fails here even with copy_function=copyfile: it always finishes by
        # calling copystat on every directory it created.
        source = tmp_path / "skill"
        (source / "nested").mkdir(parents=True)
        (source / "SKILL.md").write_text("skill\n", encoding="utf-8")
        (source / "nested" / "extra.md").write_text("extra\n", encoding="utf-8")

        workspace.copy_template_tree(source, tmp_path / "out")
        assert (tmp_path / "out" / "SKILL.md").read_text(encoding="utf-8") == "skill\n"
        assert (tmp_path / "out" / "nested" / "extra.md").read_text(encoding="utf-8") == "extra\n"

    def test_tree_copy_merges_into_an_existing_directory(self, tmp_path):
        # The symlink fallbacks copy into a directory that may already hold files.
        source = tmp_path / "skill"
        source.mkdir()
        (source / "SKILL.md").write_text("new\n", encoding="utf-8")
        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "keep.md").write_text("keep\n", encoding="utf-8")

        workspace.copy_template_tree(source, destination)
        assert (destination / "SKILL.md").read_text(encoding="utf-8") == "new\n"
        assert (destination / "keep.md").read_text(encoding="utf-8") == "keep\n"


class TestExecutableBit:
    """
    A filesystem that refuses chmod must not take the whole launch down.

    `install_git_hooks` called `hook.chmod(0o755)` unguarded. A WSL DrvFs mount of a Windows
    drive rejects chmod with EPERM while already reporting every file as `rwxrwxrwx`, so the
    call was ceremony that crashed `kiln .` against a `/mnt/c/...` project outright.
    """

    def _deny_chmod(self, monkeypatch):
        import os

        def deny(*_args, **_kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "chmod", deny)

    def test_silent_when_the_file_is_executable_anyway(self, tmp_path, monkeypatch, caplog):
        import logging
        import os

        target = tmp_path / "pre-push"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        self._deny_chmod(monkeypatch)
        monkeypatch.setattr(os, "access", lambda *_a, **_k: True)

        with caplog.at_level(logging.WARNING):
            workspace.make_executable(target)
        assert "could not make" not in caplog.text

    def test_warns_when_the_file_really_is_not_executable(self, tmp_path, monkeypatch, caplog):
        import logging
        import os

        target = tmp_path / "pre-push"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        self._deny_chmod(monkeypatch)
        monkeypatch.setattr(os, "access", lambda *_a, **_k: False)

        with caplog.at_level(logging.WARNING):
            workspace.make_executable(target)
        assert "could not make" in caplog.text
        assert "silently skip" in caplog.text

    def test_hook_install_survives_a_filesystem_refusing_chmod(self, paths, monkeypatch, repo):
        import os

        self._deny_chmod(monkeypatch)
        monkeypatch.setattr(os, "access", lambda *_a, **_k: True)
        monkeypatch.setattr(workspace.os, "name", "posix")

        hook = workspace.install_git_hooks(paths)
        assert hook is not None and hook.is_file()
