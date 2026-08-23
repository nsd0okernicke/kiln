from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.handoff import (
    GUIDANCE_HEADING,
    append_trail_entry,
    attach_guidance,
    format_handoff,
    parse_guidance,
    parse_handoff,
    strip_guidance,
)

line = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_./",
    max_size=60,
)
nonempty_line = line.filter(lambda value: bool(value.strip()))
trail = st.lists(nonempty_line, max_size=8).map(tuple)


@given(
    sender=nonempty_line,
    handoff=nonempty_line,
    branch=nonempty_line,
    commit=nonempty_line,
    summary=line,
    next_role=nonempty_line,
    entries=trail,
    ping=st.booleans(),
    escalation=st.booleans(),
)
def test_formatted_handoffs_round_trip_their_routing_fields(
    sender: str,
    handoff: str,
    branch: str,
    commit: str,
    summary: str,
    next_role: str,
    entries: tuple[str, ...],
    ping: bool,
    escalation: bool,
) -> None:
    rendered = format_handoff(
        sender=sender,
        handoff=handoff,
        branch=branch,
        commit=commit,
        summary=summary,
        next_role=next_role,
        timestamp="2026-08-23T12:00:00Z",
        ping=ping,
        trail=entries,
        escalation=escalation,
    )
    parsed = parse_handoff(rendered)

    assert (parsed.sender, parsed.handoff, parsed.branch, parsed.commit) == (
        sender.strip(),
        handoff.strip(),
        branch.strip(),
        commit.strip(),
    )
    assert parsed.is_ping is ping
    assert parsed.trail == (tuple(entry.strip() for entry in entries) if ping else ())


@given(content=st.text(max_size=300), first=line, replacement=line)
def test_attaching_guidance_replaces_the_previous_guidance(
    content: str, first: str, replacement: str
) -> None:
    base = strip_guidance(content)
    once = attach_guidance(base, first)
    twice = attach_guidance(once, replacement)

    assert parse_guidance(twice) == replacement.strip()
    assert strip_guidance(twice).rstrip() == base.rstrip()
    assert twice.count(GUIDANCE_HEADING) == 1


@given(entries=trail, role=nonempty_line, branch=nonempty_line)
def test_appending_a_trail_entry_preserves_the_existing_trail(
    entries: tuple[str, ...], role: str, branch: str
) -> None:
    result = append_trail_entry(entries, role, branch)
    assert result[:-1] == entries
    assert result[-1] == f"{role} ({branch})"
