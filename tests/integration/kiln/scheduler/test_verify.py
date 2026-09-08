"""
The per-role verification command: a real gate where there was only prose.

`run` shells out for real here rather than mocking subprocess — the whole point of the
feature is that an opaque command's exit code is trusted, and a test that stubs the exit code
would verify nothing about that. The commands used are trivial and cross-platform.
"""

from __future__ import annotations

import sys

import pytest

from kiln.scheduler.infrastructure.diagnostics import verification as verify

pytestmark = pytest.mark.integration

PASS = f'"{sys.executable}" -c "pass"'
FAIL = f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(3)"'


class TestRun:
    def test_a_zero_exit_passes(self, tmp_path):
        assert verify.run(PASS, tmp_path).ok is True

    def test_a_non_zero_exit_fails(self, tmp_path):
        assert verify.run(FAIL, tmp_path).ok is False

    def test_the_output_is_kept_for_the_retry(self, tmp_path):
        # The point of failing: the worker has to be told what broke.
        assert "boom" in verify.run(FAIL, tmp_path).output

    def test_the_exit_code_is_reported(self, tmp_path):
        assert "exited 3" in verify.run(FAIL, tmp_path).output

    def test_it_runs_in_the_given_directory(self, tmp_path):
        (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
        script = "import pathlib,sys; sys.exit(0 if pathlib.Path('marker.txt').is_file() else 1)"
        assert verify.run(f'"{sys.executable}" -c "{script}"', tmp_path).ok is True

    def test_shared_report_placeholder_is_expanded_and_created(self, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        command = (
            f'"{sys.executable}" -c '
            "\"import pathlib; pathlib.Path(r'{reports}/junit.xml').write_text('ok')\""
        )

        result = verify.run(command, worktree, project_root=tmp_path)

        assert result.ok is True
        assert (tmp_path / "reports" / "junit.xml").read_text() == "ok"

    def test_project_placeholder_is_shell_quoted(self, tmp_path):
        root = tmp_path / "project with spaces"
        root.mkdir()

        expanded = verify._expand_paths("tool --root {project}", root)

        assert str(root) in expanded
        assert expanded != f"tool --root {root}"

    def test_a_hang_is_killed_and_treated_as_a_failure(self, tmp_path):
        # Not a scheduler crash: a role must not die over its own quality gate.
        hang = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        result = verify.run(hang, tmp_path, timeout=1)
        assert result.ok is False
        assert result.timed_out is True

    def test_an_unknown_command_is_a_failure_not_an_exception(self, tmp_path):
        # A typo in a profile's verify command must fail the gate, not crash the role. The
        # shell reports it as a non-zero exit (1 on cmd, 127 on POSIX); either way it fails.
        result = verify.run("kiln-no-such-command-exists --please", tmp_path)
        assert result.ok is False
        assert result.output

    def test_it_does_not_pass_an_llm_base_url_through(self, tmp_path, monkeypatch):
        # A worker may have left these pointing at the capture kiln.proxy. Verification is not an
        # agent call and has no business inheriting one.
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
        command = (
            f'"{sys.executable}" -c '
            "\"import os,sys; sys.exit(1 if os.environ.get('ANTHROPIC_BASE_URL') else 0)\""
        )
        assert verify.run(command, tmp_path).ok is True


class TestSummary:
    def test_a_pass_says_so(self):
        assert verify.VerifyResult(ok=True, output="").summary == "verification passed"

    def test_a_failure_leads_with_the_first_meaningful_line(self):
        result = verify.VerifyResult(ok=False, output="\n\n3 tests failed\nmore detail")
        assert result.summary == "verification failed: 3 tests failed"

    def test_a_timeout_says_so_rather_than_just_failed(self):
        # An operator needs to tell "the tests are red" from "the suite never finished".
        result = verify.VerifyResult(ok=False, output="", timed_out=True)
        assert "timed out" in result.summary


class TestTail:
    def test_short_output_survives_whole(self):
        assert verify.tail("one\ntwo") == "one\ntwo"

    def test_it_keeps_the_end_not_the_beginning(self):
        # Every test runner worth the name puts its summary at the bottom; the first forty
        # lines of a failing suite are usually collection noise.
        text = "\n".join(str(n) for n in range(200))
        assert verify.tail(text, max_lines=3).endswith("197\n198\n199")

    def test_it_says_how_much_it_dropped(self):
        text = "\n".join(str(n) for n in range(200))
        assert "197 earlier line(s) omitted" in verify.tail(text, max_lines=3)

    def test_one_pathological_line_is_still_capped(self):
        # The line cap alone does not bound the size; a single line can be megabytes.
        assert len(verify.tail("x" * 100_000, max_chars=500)) < 600


class TestProvenance:
    """
    A gate result is a claim about a commit. Without the commit and the tree state it is
    only a claim about a directory at a moment, which nobody can reproduce or audit.
    """

    def test_a_pass_names_the_commit_it_was_measured_on(self, git_repo):
        result = verify.run(PASS, git_repo, project_root=git_repo)

        assert result.commit_sha != ""
        assert result.commit_sha[:12] in result.summary

    def test_a_directory_under_no_version_control_names_no_commit(self, tmp_path):
        # It is still marked dirty: nothing here could be checked against a commit, and
        # "could not tell" is recorded as unverified rather than as clean.
        result = verify.run(PASS, tmp_path, project_root=tmp_path)

        assert result.commit_sha == ""
        assert result.summary == "verification passed [dirty tree]"

    def test_an_uncommitted_change_is_marked_on_a_pass_too(self, git_repo):
        # A green gate measured on a dirty tree is the dangerous one: it reads as evidence
        # for the commit, and it is not.
        (git_repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        result = verify.run(PASS, git_repo, project_root=git_repo)

        assert result.tree_clean is False
        assert "[dirty tree]" in result.summary

    def test_a_clean_tree_is_not_annotated(self, git_repo):
        assert "[dirty tree]" not in verify.run(PASS, git_repo, project_root=git_repo).summary

    def test_a_directory_git_cannot_read_is_reported_as_not_clean(self, tmp_path):
        # "Could not tell" has to fall on the cautious side: claiming a clean tree that was
        # never checked is exactly the false evidence this exists to prevent.
        assert verify._check_tree_clean(tmp_path / "absent") is False

    def test_an_unreachable_head_leaves_the_commit_blank(self, tmp_path):
        assert verify._read_commit_sha(tmp_path / "absent") == ""


class TestRunClean:
    """`run_clean` refuses to record a result at all when the tree does not match HEAD."""

    def test_a_clean_tree_passes_the_result_through_untouched(self, git_repo):
        assert verify.run_clean(PASS, git_repo, project_root=git_repo).ok is True

    def test_a_dirty_tree_fails_even_when_the_command_passed(self, git_repo):
        (git_repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        result = verify.run_clean(PASS, git_repo, project_root=git_repo)

        assert result.ok is False
        assert "working tree is dirty" in result.output

    def test_the_refusal_keeps_the_commit_and_the_original_output(self, git_repo):
        # Whoever reads the escalation needs both halves: what the gate said, and why the
        # answer is being thrown away anyway.
        (git_repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        result = verify.run_clean(FAIL, git_repo, project_root=git_repo)

        assert result.commit_sha != "" and result.tree_clean is False
        assert "boom" in result.output


class TestDecode:
    """A killed process hands back bytes even when the run asked for text."""

    def test_bytes_are_decoded(self):
        assert verify._decode(b"partial output") == "partial output"

    def test_undecodable_bytes_do_not_raise(self):
        # The stream is cut wherever the timeout landed, so a half-written character is
        # normal; losing the whole tail to a UnicodeDecodeError is not acceptable.
        assert verify._decode(b"caf\xe9") != ""

    def test_text_is_passed_through(self):
        assert verify._decode("already text") == "already text"

    def test_nothing_captured_is_an_empty_string_not_none(self):
        assert verify._decode(None) == ""
