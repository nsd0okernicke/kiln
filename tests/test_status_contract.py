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


class TestHandoffName:
    """
    The `KILN-HANDOFF:` sentinel exists because in scheduler mode the *scheduler* composes the
    outbound message, copying `Handoff:` from the inbound verbatim -- so the specifier had no
    channel to name anything, and every message in a live project's queue ended up grouped
    under the `pending` placeholder it was supposed to replace.
    """

    def test_a_name_is_read_from_the_sentinel(self):
        stdout = "KILN-HANDOFF: cat-3-search\nKILN-STATUS: done wrote the spec"
        assert sc.parse_worker_report(stdout).handoff_name == "cat-3-search"

    def test_no_sentinel_means_no_name(self):
        assert sc.parse_worker_report("KILN-STATUS: done ok").handoff_name == ""

    def test_the_prefix_is_case_insensitive(self):
        # Same leniency the status sentinel already grants; a successful cycle must not be
        # thrown away over capitalisation.
        stdout = "kiln-handoff: fix-isbn\nKILN-STATUS: done ok"
        assert sc.parse_worker_report(stdout).handoff_name == "fix-isbn"

    def test_quotes_are_stripped(self):
        stdout = 'KILN-HANDOFF: "cat-3-search"\nKILN-STATUS: done ok'
        assert sc.parse_worker_report(stdout).handoff_name == "cat-3-search"

    def test_the_last_one_wins(self):
        # Matching the status sentinel's rule: a worker quoting the contract earlier in its
        # narrative must not shadow its real answer.
        stdout = "KILN-HANDOFF: draft\nKILN-HANDOFF: final-name\nKILN-STATUS: done ok"
        assert sc.parse_worker_report(stdout).handoff_name == "final-name"

    def test_the_placeholder_itself_is_rejected(self):
        # Answering `pending` is answering nothing; storing it is the bug being fixed.
        stdout = "KILN-HANDOFF: pending\nKILN-STATUS: done ok"
        assert sc.parse_worker_report(stdout).handoff_name == ""

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "-leading-dash",
            "has'a quote",
            "semi;colon",
            "a name that is far too long " + "x" * 80,
            "I have named this work the author search feature; see the spec for details",
        ],
    )
    def test_a_name_that_could_poison_the_grouping_key_is_rejected(self, value):
        # It becomes a database grouping key and appears in log lines and commit subjects,
        # so a worker answering with a sentence must contribute nothing rather than become
        # the key everything is grouped by.
        stdout = f"KILN-HANDOFF: {value}\nKILN-STATUS: done ok"
        assert sc.parse_worker_report(stdout).handoff_name == ""

    def test_a_blocked_worker_names_nothing(self):
        # There is no work to name; the cycle did not produce any.
        stdout = "KILN-HANDOFF: cat-3-search\nKILN-STATUS: blocked no fixtures"
        assert sc.parse_worker_report(stdout).handoff_name == ""

    def test_it_does_not_disturb_the_status_sentinel(self):
        stdout = "KILN-HANDOFF: cat-3-search\nKILN-STATUS: done wrote the spec"
        result = sc.parse_worker_report(stdout)
        assert result.status == sc.STATUS_DONE
        assert result.summary == "wrote the spec"


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

    def test_the_instruction_documents_the_handoff_sentinel(self):
        assert sc.HANDOFF_PREFIX in sc.WORKER_STATUS_INSTRUCTION

    def test_the_instructions_example_name_survives_its_own_parser(self):
        # The drift guard that matters here: workers are shown example names, and an example
        # the validator would reject would teach every worker to be silently ignored.
        examples = [
            line.strip()
            for line in sc.WORKER_STATUS_INSTRUCTION.splitlines()
            if line.strip().upper().startswith(sc.HANDOFF_PREFIX)
        ]
        assert examples, "the instruction must show at least one example"
        for line in examples:
            if "<" in line:
                continue  # the placeholder form, not a real name
            assert sc.parse_handoff_name(line), f"{line!r} would be rejected"

    def test_every_name_the_instruction_recommends_is_accepted(self):
        for name in ("cat-3-search-by-author", "fix-isbn-validation"):
            assert name in sc.WORKER_STATUS_INSTRUCTION
            assert sc.parse_handoff_name(f"{sc.HANDOFF_PREFIX} {name}") == name

    def test_the_validator_and_the_sentinel_parser_agree(self):
        # `is_valid_work_item_name` is public because the worker's sentinel is no longer the
        # only untrusted source -- the cockpit lets a human type a name too. The two paths
        # must accept the same set, or a name valid in the browser would be dropped by a
        # worker echoing it back.
        for name in ("cat-3-search-by-author", "CAT-3", "fix isbn/validation", "a"):
            assert sc.is_valid_work_item_name(name), name
            assert sc.parse_handoff_name(f"{sc.HANDOFF_PREFIX} {name}") == name

    @pytest.mark.parametrize(
        "name",
        [
            "please restart this with the CAT-3 spec, thanks!",  # a sentence
            "-leading-hyphen",                                   # must start alphanumeric
            "",
            "x" * 81,                                            # over the 80-char budget
            'quote"inside',
        ],
    )
    def test_a_name_that_would_poison_the_grouping_key_is_rejected(self, name):
        assert not sc.is_valid_work_item_name(name)

    def test_the_placeholder_is_left_for_the_caller_to_judge(self):
        # A worker echoing `pending` failed to name anything; a human choosing it means
        # "let the specifier name this". Same string, opposite verdicts, so the shared
        # validator stays out of it.
        assert sc.is_valid_work_item_name(sc.PENDING_HANDOFF)
        assert sc.parse_handoff_name(f"{sc.HANDOFF_PREFIX} {sc.PENDING_HANDOFF}") == ""

    def test_cli_prints_instruction(self, capsys):
        assert sc._main(["--instruction"]) == 0
        assert capsys.readouterr().out == sc.WORKER_STATUS_INSTRUCTION

    def test_cli_without_args_reports_failure(self, capsys):
        assert sc._main([]) == 1
        assert "instruction" in capsys.readouterr().out
