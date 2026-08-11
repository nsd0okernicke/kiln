"""
Handoff message parsing and formatting.

Codifies the "Handoff Message Format" section of constitution/workflow.md, which is today
followed by hand via `/kiln-receive` and `/kiln-handoff`. Both directions live here so an
outbound message this module writes is always readable by the parser it ships with — and,
critically, by the legacy wrapper roles that still read these messages as prose.

Pure string in, dataclass out: no I/O, no clock. The caller supplies the timestamp.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The banner rule from workflow.md's template. Kept byte-identical so a downstream
#: legacy-wrapper role cannot tell a scheduler-produced handoff from a hand-written one.
SEPARATOR = "═" * 64

PING_FIELD = "Kiln-Ping"
ESCALATION_FIELD = "Kiln-Escalation"

_TRAIL_ENTRY_RE = re.compile(r"^\s*-\s+(?P<entry>.+?)\s*$")

#: A `Commit:` value only means "merge this" when it actually looks like a git object name.
#: Senders that have nothing to merge — a human's first request, a ping — do not reliably
#: leave the field empty; they write prose like `(none — human request, no prior commit)`.
#: Handing that to `git merge` fails and escalates a cycle that was never broken.
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _field(content: str, name: str) -> str:
    """Read a `Name: value` header field, or '' when absent."""
    match = re.search(rf"^{re.escape(name)}:[ \t]*(?P<value>.*)$", content, re.MULTILINE)
    return match.group("value").strip() if match else ""


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1"}


@dataclass(frozen=True)
class InboundHandoff:
    """The four routing-relevant fields of an inbound message, plus ping state."""

    sender: str
    handoff: str
    branch: str
    commit: str
    is_ping: bool
    trail: tuple[str, ...]
    raw: str

    @property
    def is_mergeable(self) -> bool:
        """True when there is a commit to merge. Pings legitimately carry no commit."""
        return bool(_COMMIT_RE.match(self.commit))


def parse_handoff(content: str) -> InboundHandoff:
    """
    Extract the header fields from an inbound message.

    Deliberately tolerant: missing fields become empty strings rather than raising, because
    the scheduler must be able to report *which* field a malformed message was missing
    instead of dying with a parse error the operator has to reverse-engineer.
    """
    return InboundHandoff(
        sender=_field(content, "Sender"),
        handoff=_field(content, "Handoff"),
        branch=_field(content, "Branch"),
        commit=_field(content, "Commit"),
        is_ping=_is_truthy(_field(content, PING_FIELD)),
        trail=parse_trail(content),
        raw=content,
    )


def is_escalation(content: str) -> bool:
    """
    True when a message carries `Kiln-Escalation: true`.

    Public because the inbox needs it to decide whether a human is looking at a routine
    handoff or at a swarm that has stopped and is asking for help.
    """
    return _is_truthy(_field(content, ESCALATION_FIELD))


def parse_trail(content: str) -> tuple[str, ...]:
    """
    Read the `Trail:` list of a kiln-ping message.

    Only the bulleted lines immediately following `Trail:` are taken, so bullets elsewhere
    in the message body are not mistaken for trail entries.
    """
    match = re.search(r"^Trail:[ \t]*$", content, re.MULTILINE)
    if not match:
        return ()

    entries: list[str] = []
    for line in content[match.end() :].splitlines():
        if not line.strip():
            if entries:
                break
            continue
        entry = _TRAIL_ENTRY_RE.match(line)
        if not entry:
            break
        entries.append(entry.group("entry"))
    return tuple(entries)


def format_handoff(
    *,
    sender: str,
    handoff: str,
    branch: str,
    commit: str,
    summary: str,
    next_role: str,
    timestamp: str,
    ping: bool = False,
    trail: tuple[str, ...] = (),
    escalation: bool = False,
) -> str:
    """
    Render an outbound message in workflow.md's mandated format.

    `handoff` is the specifier's stable handoff name and must be carried through unchanged
    from the inbound message — it is what ties a whole cycle together across roles.
    """
    lines = [
        f"Sender: {sender}",
        f"Handoff: {handoff}",
        f"Branch: {branch}",
        f"Commit: {commit}",
    ]
    if ping:
        lines.append(f"{PING_FIELD}: true")
    if escalation:
        # Additive field; existing parsers ignore what they do not look for.
        lines.append(f"{ESCALATION_FIELD}: true")

    lines += [
        "",
        SEPARATOR,
        f"✓ {sender.upper()} HANDOFF — {timestamp}",
        SEPARATOR,
        summary,
        "",
    ]

    if ping:
        lines.append("Trail:")
        lines.extend(f"- {entry}" for entry in trail)
        lines.append("")

    lines.append(f"Next role: {next_role}")
    return "\n".join(lines)


def append_trail_entry(trail: tuple[str, ...], role: str, branch: str) -> tuple[str, ...]:
    """Append this role's hop to a ping trail, per kiln-receive/SKILL.md step 3."""
    return (*trail, f"{role} ({branch})")
