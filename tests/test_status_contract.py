"""
The sentinel parser stands in for the one LLM judgment call the scheduler removes, so its
edge cases are the difference between a swarm cycle advancing and stalling.
"""

from __future__ import annotations

import pytest
from scheduler import status_contract as sc


class TestRecognisedSentinels:
    def test_done_with_summary(self):
        result = sc.parse_worker_report("KILN-STATUS: done Added order intake tests")
        assert result.status == sc.STATUS_DONE
        assert result.summary == "Added order intake tests"
        assert result.sentinel_found is True
        assert result.is_done and not result.is_blocked

    def test_blocked_with_reason(self):
        result = sc.parse_worker_report("KILN-STATUS: blocked missing acceptance criteria")
        assert result.status == sc.STATUS_BLOCKED
        assert result.summary == "missing acceptance criteria"
        assert result.is_blocked and not result.is_done

    def test_status_without_summary_is_still_valid(self):
        result = sc.parse_worker_report("KILN-STATUS: done")
        assert result.is_done
        assert result.summary == ""
        assert result.sentinel_found is True

    @pytest.mark.parametrize(
        "line",
        [
            "KILN-STATUS: done ok",
            "kiln-status: done ok",
            "Kiln-Status: DONE ok",
            "KILN-STATUS:done ok",
            "   KILN-STATUS:   done   ok",
        ],
        ids=["canonical", "lowercase", "mixed-case", "no-space", "extra-whitespace"],
    )
    def test_lenient_matching(self, line):
        # Leniency is deliberate: a successful run must never be misread as a failure
        # over casing or spacing.
        assert sc.parse_worker_report(line).is_done

    def test_summary_internal_whitespace_preserved(self):
        result = sc.parse_worker_report("KILN-STATUS: done fixed  double  spacing")
        assert result.summary == "fixed  double  spacing"


class TestSentinelLocation:
    def test_trailing_blank_lines_tolerated(self):
        assert sc.parse_worker_report("work\nKILN-STATUS: done ok\n\n   \n").is_done

    def test_last_sentinel_wins(self):
        stdout = "KILN-STATUS: blocked first attempt\nretried\nKILN-STATUS: done second attempt"
        result = sc.parse_worker_report(stdout)
        assert result.is_done
        assert result.summary == "second attempt"

    def test_narrative_quoting_the_contract_does_not_shadow_the_verdict(self):
        stdout = (
            "I was told to end with KILN-STATUS: blocked if I could not proceed.\n"
            "I could proceed.\n"
            "KILN-STATUS: done implemented the feature"
        )
        assert sc.parse_worker_report(stdout).is_done

    def test_sentinel_need_not_be_the_final_line(self):
        # Some CLIs append their own trailer after the agent's output. Scanning backwards
        # skips non-sentinel lines, so the real verdict is still found.
        result = sc.parse_worker_report("KILN-STATUS: done ok\n[session ended]")
        assert result.is_done
        assert result.summary == "ok"


class TestFailureModes:
    @pytest.mark.parametrize(
        "stdout",
        ["", "   ", "no sentinel here", "work done successfully!", "KILN STATUS: done"],
        ids=["empty", "whitespace", "plain-text", "claims-success", "missing-hyphen"],
    )
    def test_missing_sentinel_is_blocked_not_a_crash(self, stdout):
        result = sc.parse_worker_report(stdout)
        assert result.is_blocked
        assert result.sentinel_found is False
        assert result.summary == sc.MISSING_SENTINEL_SUMMARY

    def test_truncated_output_is_blocked(self):
        # A worker killed mid-sentence never reaches its sentinel.
        result = sc.parse_worker_report("started work\nKILN-STAT")
        assert result.is_blocked
        assert result.sentinel_found is False

    @pytest.mark.parametrize("word", ["finished", "success", "failed", "done!", "42"])
    def test_unrecognised_status_is_blocked_but_records_the_sentinel(self, word):
        result = sc.parse_worker_report(f"KILN-STATUS: {word} some detail")
        assert result.is_blocked
        # sentinel_found stays True: "worker reported nonsense" and "worker reported
        # nothing" are different bugs, and the escalation message should say which.
        assert result.sentinel_found is True
        assert word in result.summary
        assert "some detail" in result.summary

    def test_unrecognised_status_without_detail(self):
        result = sc.parse_worker_report("KILN-STATUS: finished")
        assert result.is_blocked
        assert result.summary == "unrecognised status 'finished'; treated as blocked"

    def test_bare_sentinel_prefix_is_blocked(self):
        result = sc.parse_worker_report("KILN-STATUS:")
        assert result.is_blocked
        assert result.sentinel_found is True


class TestResultType:
    def test_result_is_immutable(self):
        result = sc.parse_worker_report("KILN-STATUS: done ok")
        with pytest.raises(AttributeError):
            result.status = sc.STATUS_BLOCKED  # type: ignore[misc]


class TestInstructionText:
    def test_instruction_documents_both_statuses(self):
        text = sc.WORKER_STATUS_INSTRUCTION
        assert f"{sc.SENTINEL_PREFIX} {sc.STATUS_DONE}" in text
        assert f"{sc.SENTINEL_PREFIX} {sc.STATUS_BLOCKED}" in text

    def test_instruction_examples_round_trip_through_the_parser(self):
        # The guard against parser/instruction drift: every example the workers are shown
        # must actually parse to the status it claims to demonstrate.
        examples = [
            line.strip()
            for line in sc.WORKER_STATUS_INSTRUCTION.splitlines()
            if line.strip().upper().startswith(sc.SENTINEL_PREFIX)
        ]
        assert len(examples) == 2
        statuses = {sc.parse_worker_report(line).status for line in examples}
        assert statuses == {sc.STATUS_DONE, sc.STATUS_BLOCKED}

    def test_cli_prints_instruction(self, capsys):
        assert sc._main(["--instruction"]) == 0
        assert capsys.readouterr().out == sc.WORKER_STATUS_INSTRUCTION

    def test_cli_without_args_reports_failure(self, capsys):
        assert sc._main([]) == 1
        assert "instruction" in capsys.readouterr().out
