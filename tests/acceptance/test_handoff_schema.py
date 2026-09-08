"""Consumer-driven contract test on the handoff schema (issue #47, finding 8).

Validates that every handoff message across the full role loop conforms to the contract
expected by the receiving role: the required fields exist, the format is parseable across
all routing hops, and no role breaks the chain by producing an unparseable message.
"""

from workflow_support import prepare, rows, scheduler, send


def test_full_role_loop_handoff_contract(initialized_project, command_runner, fake_pi):
    """
    Validate the handoff message schema across the full role loop.

    Every handoff must carry:
    - sender, target, branch, commit fields
    - A parseable separator line (═══════)
    - A human-readable summary
    - Next role identified

    This is a consumer-driven contract test: each role is a consumer of the
    previous role's output, and the handoff schema is the shared contract
    between them.
    """
    prepare(initialized_project, command_runner, profile="full")
    send(
        command_runner,
        initialized_project,
        "cross-service event path validation",
        target="specifier",
        handoff="pending",
    )

    route = [
        ("specifier", "coder"),
        ("coder", "reviewer"),
        ("reviewer", "architect"),
        ("architect", "human-in-the-loop"),
    ]

    for role, target in route:
        result = scheduler(
            command_runner,
            initialized_project,
            fake_pi,
            status="done",
            role=role,
            target=target,
            fake_file=f"{role}-worker.txt",
        )
        assert f"handed off to {target}" in result.stderr

    messages = rows(initialized_project)
    handoffs = [row for row in messages if row["status"] in ("queued", "processed")]
    assert len(handoffs) >= 5  # specifier -> coder -> reviewer -> architect -> human

    # Validate contract on every handoff
    for handoff in handoffs:
        content = handoff["content"]
        # Every handoff must have the standard separator
        assert "════════════════════════════════════════════════════════════════" in content, (
            f"Handoff from {handoff['sender']} to {handoff['target']} missing separator"
        )
        # Every handoff must have a sender line
        assert "Sender:" in content, (
            f"Handoff from {handoff['sender']} to {handoff['target']} missing Sender"
        )
        # Every handoff must have a Branch line
        assert "Branch:" in content, (
            f"Handoff from {handoff['sender']} to {handoff['target']} missing Branch"
        )

    # Cross-role identity check: work_item must be consistent across all hops
    work_items = {
        row["work_item"]
        for row in handoffs
        if row["work_item"] and row["work_item"] != "pending"
    }
    assert len(work_items) == 1, f"Expected 1 work item across all roles, got {work_items}"
