"""
Workspace setup against real git repositories.

Most assertions here encode a failure that has actually happened: a tracked `.kiln` symlink
breaking later merges, a stray `kiln/.gitignore` hiding the whole constitution from every
worktree, a BOM making git refuse to run the pre-push hook.
"""

from __future__ import annotations

import pytest
from launcher import workspace
from launcher.config import parse_profile
from launcher.paths import KilnPaths

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
    (framework / "kiln" / ".claude").mkdir(parents=True)
    (framework / "kiln" / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (framework / "kiln" / "framework" / "tools").mkdir(parents=True)
    (framework / "kiln" / "framework" / "tools" / "set-status.py").write_text(
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
        return json.loads(text[text.index("{"):])["trustedFolders"]

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

    def test_missing_config_file_is_a_warning_not_a_crash(self, paths, tmp_path, monkeypatch, caplog):
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
        assert lines[0].split("\t") == ["1", "specifier", "claude", "Specifier"]
