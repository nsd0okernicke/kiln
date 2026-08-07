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
