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
            '"import os,sys; sys.exit(1 if os.environ.get(\'ANTHROPIC_BASE_URL\') else 0)"'
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
