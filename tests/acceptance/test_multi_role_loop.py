"""Acceptance scenario: preserve one work item across the full autonomous role loop."""

from workflow_support import prepare, rows, scheduler, send


def test_full_role_loop_preserves_identity(initialized_project, command_runner, fake_claude):
    prepare(initialized_project, command_runner, profile="full")
    send(
        command_runner,
        initialized_project,
        "take this through the full loop",
        target="specifier",
        handoff="pending",
    )

    route = [
        ("specifier", "coder"),
        ("coder", "refactorer"),
        ("refactorer", "architect"),
        ("architect", "human-in-the-loop"),
    ]
    for role, target in route:
        result = scheduler(
            command_runner,
            initialized_project,
            fake_claude,
            status="done",
            role=role,
            target=target,
            fake_file=f"{role}-worker.txt",
        )
        assert f"handed off to {target}" in result.stderr

    messages = rows(initialized_project)
    role_messages = [row for row in messages if row["sender"] in {role for role, _ in route}]
    assert [row["target"] for row in role_messages] == [target for _, target in route]
    assert {row["work_item"] for row in role_messages} == {"system-test-task"}
    assert all(row["status"] == "processed" for row in messages[:-1])
    assert messages[-1]["status"] == "queued"
