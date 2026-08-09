"""
Git operations against real repositories. These replace steps the wrapper LLM used to
perform by following prose, so the failure modes it handled by judgement (conflicts,
nothing to squash) must now be handled by code.
"""

from __future__ import annotations

import pytest
from scheduler import git_ops

pytestmark = pytest.mark.integration


def _write_commit(repo, git_cmd, filename, content, message):
    (repo / filename).write_text(content, encoding="utf-8")
    git_cmd(repo, "add", "-A")
    git_cmd(repo, "commit", "-qm", message)
    return git_ops.head_commit(repo)


class TestBasics:
    def test_head_and_branch(self, git_repo):
        assert len(git_ops.head_commit(git_repo)) == 40
        assert git_ops.current_branch(git_repo) == "main"

    def test_head_of_non_repo_is_empty(self, tmp_path):
        assert git_ops.head_commit(tmp_path) == ""

    def test_failed_command_does_not_raise(self, tmp_path):
        result = git_ops.run_git(["rev-parse", "HEAD"], tmp_path)
        assert result.ok is False
        assert result.returncode != 0
        assert result.output

    def test_pending_changes_detection(self, git_repo):
        assert git_ops.has_pending_changes(git_repo) is False
        (git_repo / "new.txt").write_text("x", encoding="utf-8")
        assert git_ops.has_pending_changes(git_repo) is True


class TestMerge:
    def test_merges_a_sender_branch(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "from sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")

        result = git_ops.merge_commit(sender_commit, git_repo)

        assert result.ok
        assert (git_repo / "f.txt").read_text(encoding="utf-8") == "from sender"

    def test_creates_a_merge_commit_usable_as_the_squash_anchor(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")
        _write_commit(git_repo, git_cmd, "own.txt", "mine", "own work")

        git_ops.merge_commit(sender_commit, git_repo)

        assert git_ops.squash_anchor(git_repo) == git_ops.head_commit(git_repo)

    def test_never_fast_forwards(self, git_repo, git_cmd):
        # A fast-forward would leave no merge commit, so squash_anchor would fall back to
        # the ROOT commit and the next squash would collapse the whole project history.
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")  # main has NOT diverged

        git_ops.merge_commit(sender_commit, git_repo)

        head = git_ops.head_commit(git_repo)
        parents = git_ops.run_git(["rev-list", "--parents", "-n", "1", "HEAD"], git_repo).stdout
        assert len(parents.split()) == 3, "merge must have two parents, not fast-forward"
        assert git_ops.squash_anchor(git_repo) == head

    def test_conflict_fails_and_leaves_no_half_merged_tree(self, git_repo, git_cmd):
        # A conflicted tree left behind would poison every later cycle.
        _write_commit(git_repo, git_cmd, "shared.txt", "main version", "main edit")
        git_cmd(git_repo, "checkout", "-q", "-b", "sender", "HEAD~1")
        conflicting = _write_commit(git_repo, git_cmd, "shared.txt", "sender version", "sender")
        git_cmd(git_repo, "checkout", "-q", "main")

        result = git_ops.merge_commit(conflicting, git_repo)

        assert result.ok is False
        assert result.output
        assert git_ops.has_pending_changes(git_repo) is False

    def test_unknown_commit_fails_cleanly(self, git_repo):
        assert git_ops.merge_commit("0" * 40, git_repo).ok is False

    def test_custom_message_replaces_gits_generic_default(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "from sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")

        result = git_ops.merge_commit(
            sender_commit, git_repo, message="[Coder] Merge CAT-3 from specifier\n\nCommit: x"
        )

        assert result.ok
        subject = git_ops.run_git(["log", "-1", "--format=%s"], git_repo).stdout
        assert subject == "[Coder] Merge CAT-3 from specifier"
        assert "Merge commit" not in subject


class TestSquashMergeCommit:
    """
    `human-in-the-loop`'s inbox path: land the sender's work without dragging their whole
    branch history onto `@current`, which -- unlike every scheduled role's sub-branch -- is
    the project's real, potentially-pushed branch.
    """

    def test_lands_the_senders_work(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "from sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")

        result = git_ops.squash_merge_commit(sender_commit, git_repo, "squashed")

        assert result.ok
        assert (git_repo / "f.txt").read_text(encoding="utf-8") == "from sender"

    def test_creates_no_merge_commit_no_second_parent(self, git_repo, git_cmd):
        # The whole point: this must not become something squash_anchor's `--merges` search
        # would ever find, and it must not carry the sender branch in as ancestry.
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")
        before = git_ops.head_commit(git_repo)

        result = git_ops.squash_merge_commit(sender_commit, git_repo, "squashed")

        parents = git_ops.run_git(["log", "-1", "--format=%P", result.stdout], git_repo).stdout
        assert parents == before, "must have exactly one parent: the previous HEAD"
        assert git_ops.run_git(["log", "--merges", "-1"], git_repo).stdout == "", (
            "must not be discoverable by squash_anchor's --merges search"
        )

    def test_uses_the_given_message(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender_commit = _write_commit(git_repo, git_cmd, "f.txt", "sender", "sender work")
        git_cmd(git_repo, "checkout", "-q", "main")

        git_ops.squash_merge_commit(
            sender_commit, git_repo, "[Human-in-the-loop] Merge CAT-3 from architect"
        )

        subject = git_ops.run_git(["log", "-1", "--format=%s"], git_repo).stdout
        assert subject == "[Human-in-the-loop] Merge CAT-3 from architect"

    def test_a_conflict_fails_and_leaves_a_clean_tree(self, git_repo, git_cmd):
        # git merge --squash never sets MERGE_HEAD, so the usual `merge --abort` recovery
        # would not apply here -- this is the regression that guards against that gap.
        _write_commit(git_repo, git_cmd, "shared.txt", "main version", "main edit")
        git_cmd(git_repo, "checkout", "-q", "-b", "sender", "HEAD~1")
        conflicting = _write_commit(git_repo, git_cmd, "shared.txt", "sender version", "sender")
        git_cmd(git_repo, "checkout", "-q", "main")

        result = git_ops.squash_merge_commit(conflicting, git_repo, "squashed")

        assert result.ok is False
        assert git_ops.has_pending_changes(git_repo) is False

    def test_a_noop_handoff_is_success_not_failure(self, git_repo, git_cmd):
        # Re-sent or already-applied content: nothing to commit, but that's not an error.
        commit = git_ops.head_commit(git_repo)

        result = git_ops.squash_merge_commit(commit, git_repo, "squashed")

        assert result.ok
        assert result.stdout == commit

    def test_recovers_from_generated_scaffolding_the_same_as_a_real_merge(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        (git_repo / ".claude").mkdir()
        (git_repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-q", "-m", "swept in scaffolding")
        commit = git_ops.head_commit(git_repo)
        git_cmd(git_repo, "checkout", "-q", "main")
        (git_repo / ".claude").mkdir()
        (git_repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

        assert git_ops.squash_merge_commit(commit, git_repo, "squashed").ok


class TestGeneratedScaffoldingBlockingAMerge:
    """
    Regression: the coder deadlocked on `.claude/settings.json`.

    The launcher drops that file, untracked, into every worktree. The specifier's
    `squash_since` ran `git add -A`, committed it, and the coder's next merge aborted with
    "untracked working tree files would be overwritten" — the swarm stopped dead one handoff
    in, and no retry could ever clear it.
    """

    def _sender_commits_scaffolding(self, git_repo, git_cmd, relative=".claude/settings.json"):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        target = git_repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        git_cmd(git_repo, "add", "-A")
        git_cmd(git_repo, "commit", "-q", "-m", "swept in scaffolding")
        commit = git_ops.head_commit(git_repo)
        git_cmd(git_repo, "checkout", "-q", "main")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")  # this worktree's untracked copy
        return commit

    def test_merge_recovers_instead_of_deadlocking(self, git_repo, git_cmd):
        commit = self._sender_commits_scaffolding(git_repo, git_cmd)
        assert git_ops.merge_commit(commit, git_repo).ok

    def test_real_untracked_work_is_never_deleted(self, git_repo, git_cmd):
        # All-or-nothing: one unrecognised path and the merge must fail loudly instead.
        commit = self._sender_commits_scaffolding(git_repo, git_cmd, "src/app.py")
        result = git_ops.merge_commit(commit, git_repo)

        assert result.ok is False
        assert (git_repo / "src" / "app.py").exists()

    def test_a_conflict_is_still_reported_as_a_failure(self, git_repo, git_cmd):
        # The recovery path must not swallow ordinary merge conflicts.
        _write_commit(git_repo, git_cmd, "shared.txt", "main", "main edit")
        git_cmd(git_repo, "checkout", "-q", "-b", "sender", "HEAD~1")
        conflicting = _write_commit(git_repo, git_cmd, "shared.txt", "sender", "sender")
        git_cmd(git_repo, "checkout", "-q", "main")

        assert git_ops.merge_commit(conflicting, git_repo).ok is False

    def test_the_squash_can_no_longer_sweep_scaffolding_in(self, git_repo):
        # The fix that matters: prevention, so no repo reaches the state above.
        (git_repo / ".claude").mkdir()
        (git_repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

        git_ops.ensure_generated_ignored(git_repo)

        assert git_ops.has_pending_changes(git_repo) is False


class TestGeneratedPathClassification:
    @pytest.mark.parametrize(
        "path", [".claude/settings.json", "tmp/", "tmp/handoff-in.md", "CLAUDE.md", ".mcp.json"]
    )
    def test_launcher_artefacts_are_recognised(self, path):
        assert git_ops.is_generated_path(path) is True

    @pytest.mark.parametrize(
        "path", ["src/app.py", "features/catalog/create_book.feature", "settings.json", "README.md"]
    )
    def test_project_content_is_not(self, path):
        assert git_ops.is_generated_path(path) is False

    def test_windows_separators_are_handled(self):
        # git reports forward slashes, but a caller passing an OS path must not be misread.
        assert git_ops.is_generated_path(".claude\\settings.json") is True


class TestBlockingUntrackedParsing:
    MESSAGE = (
        "error: The following untracked working tree files would be overwritten by merge:\n"
        "        .claude/settings.json\n"
        "        tmp/handoff-in.md\n"
        "Please move or remove them before you merge.\n"
        "Aborting\n"
    )

    def test_extracts_every_listed_path(self):
        assert git_ops.blocking_untracked(self.MESSAGE) == [
            ".claude/settings.json",
            "tmp/handoff-in.md",
        ]

    def test_stops_at_the_trailing_advice(self):
        assert "Please" not in " ".join(git_ops.blocking_untracked(self.MESSAGE))

    def test_other_failures_yield_nothing(self):
        assert git_ops.blocking_untracked("CONFLICT (content): Merge conflict in a.txt") == []


class TestSquashAnchor:
    def test_falls_back_to_root_commit_when_nothing_merged(self, git_repo, git_cmd):
        root = git_ops.head_commit(git_repo)
        _write_commit(git_repo, git_cmd, "a.txt", "a", "work a")
        assert git_ops.squash_anchor(git_repo) == root

    def test_prefers_the_most_recent_merge(self, git_repo, git_cmd):
        git_cmd(git_repo, "checkout", "-q", "-b", "sender")
        sender = _write_commit(git_repo, git_cmd, "f.txt", "s", "sender")
        git_cmd(git_repo, "checkout", "-q", "main")
        _write_commit(git_repo, git_cmd, "own.txt", "o", "own work")
        git_ops.merge_commit(sender, git_repo)
        merge_hash = git_ops.head_commit(git_repo)
        _write_commit(git_repo, git_cmd, "later.txt", "l", "later work")

        assert git_ops.squash_anchor(git_repo) == merge_hash


class TestSquash:
    def test_collapses_several_commits_into_one(self, git_repo, git_cmd):
        anchor = git_ops.head_commit(git_repo)
        _write_commit(git_repo, git_cmd, "a.txt", "a", "step one")
        _write_commit(git_repo, git_cmd, "b.txt", "b", "step two")

        result = git_ops.squash_since(anchor, "[Coder] did the work", git_repo)

        assert result.ok
        log = git_ops.run_git(["log", "--format=%s", f"{anchor}..HEAD"], git_repo).stdout
        assert log.splitlines() == ["[Coder] did the work"]
        assert result.stdout == git_ops.head_commit(git_repo)

    def test_preserves_the_file_contents(self, git_repo, git_cmd):
        anchor = git_ops.head_commit(git_repo)
        _write_commit(git_repo, git_cmd, "a.txt", "a", "one")
        _write_commit(git_repo, git_cmd, "b.txt", "b", "two")
        git_ops.squash_since(anchor, "[Coder] work", git_repo)
        assert (git_repo / "a.txt").read_text(encoding="utf-8") == "a"
        assert (git_repo / "b.txt").read_text(encoding="utf-8") == "b"

    def test_commits_worker_changes_left_uncommitted(self, git_repo):
        # A one-shot worker frequently edits without committing; the legacy prose assumed
        # it always committed, so this is the case that would silently lose work.
        anchor = git_ops.head_commit(git_repo)
        (git_repo / "worker-output.txt").write_text("done", encoding="utf-8")

        result = git_ops.squash_since(anchor, "[Coder] worker edits", git_repo)

        assert result.ok
        assert git_ops.has_pending_changes(git_repo) is False
        subject = git_ops.run_git(["log", "-1", "--format=%s"], git_repo).stdout
        assert subject == "[Coder] worker edits"

    def test_nothing_to_squash_reports_current_head(self, git_repo):
        # Ping cycles do no work; that is success, not failure.
        anchor = git_ops.head_commit(git_repo)
        result = git_ops.squash_since(anchor, "[Coder] nothing", git_repo)
        assert result.ok
        assert result.stdout == anchor

    def test_squash_result_is_a_real_commit(self, git_repo, git_cmd):
        anchor = git_ops.head_commit(git_repo)
        _write_commit(git_repo, git_cmd, "a.txt", "a", "one")
        result = git_ops.squash_since(anchor, "[Coder] work", git_repo)
        assert git_ops.run_git(["cat-file", "-t", result.stdout], git_repo).stdout == "commit"


class TestCommitAll:
    def test_commits_pending_changes(self, git_repo):
        (git_repo / "note.txt").write_text("x", encoding="utf-8")
        result = git_ops.commit_all("log: received handoff", git_repo)
        assert result.ok
        assert git_ops.has_pending_changes(git_repo) is False

    def test_clean_tree_is_a_no_op(self, git_repo):
        before = git_ops.head_commit(git_repo)
        result = git_ops.commit_all("nothing", git_repo)
        assert result.ok
        assert result.stdout == before
