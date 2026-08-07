"""
Handoff routing — parsed from the `## Handoff Routing` table in constitution/workflow.md.

Ports bin/kiln.ps1's Read-HandoffRoutingTable (bin/kiln.ps1:787) to Python and extends it
with an optional third column so sender-dependent routing is expressed as data:

    | Role              | Sends to          | When Sender |
    | ----------------- | ----------------- | ----------- |
    | human-in-the-loop | specifier         |             |
    | specifier         | coder             |             |
    | specifier         | human-in-the-loop | architect   |

Precedence: a rule whose `When Sender` matches wins over the rule with a blank
`When Sender`, which acts as the role's default. That is what lets the specifier's
"forward completed cycles back to the human" case stop being role-prompt prose that only
an LLM can follow.

Two deliberate divergences from the PowerShell original, both bug fixes:

1. The PowerShell regex matches role names with `\\w+`, which does not match `-`. Every
   hyphenated role — `human-in-the-loop` among them — is silently dropped from the table.
   It goes unnoticed today only because the single consumer (bin/kiln.ps1:849) defaults
   to `"specifier"`, which happens to be the right answer for that one role. Any other
   hyphenated role would be misrouted with no error.
2. The PowerShell regex scans the whole file, so any unrelated two-column markdown table
   in workflow.md would be read as routing rules. Parsing prefers the `## Handoff Routing`
   section when present, and only falls back to a whole-document scan when it is absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Heading that delimits the routing table. Matched case-insensitively.
SECTION_HEADING = "handoff routing"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(?P<body>.*)\|\s*$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")


@dataclass(frozen=True)
class RoutingRule:
    """One row of the routing table."""

    role: str
    target: str
    #: None for the role's default rule (blank `When Sender` cell).
    when_sender: str | None = None

    @property
    def is_default(self) -> bool:
        return self.when_sender is None


@dataclass(frozen=True)
class RoutingTable:
    """Ordered routing rules with sender-conditional resolution."""

    rules: tuple[RoutingRule, ...] = ()

    def resolve(self, role: str, sender: str | None = None) -> str | None:
        """
        Return the handoff target for `role`, given the inbound message's `sender`.

        A rule matching `sender` exactly takes precedence over the role's default rule.
        Returns None when the role has no rule at all — callers decide whether that is an
        error or falls back to a default target.
        """
        role_key = _normalise(role)
        sender_key = _normalise(sender) if sender is not None else None

        default: str | None = None
        for rule in self.rules:
            if rule.role != role_key:
                continue
            if rule.when_sender is None:
                if default is None:
                    default = rule.target
            elif sender_key is not None and rule.when_sender == sender_key:
                return rule.target
        return default

    def roles(self) -> tuple[str, ...]:
        """Distinct roles that appear in the table, in first-seen order."""
        seen: dict[str, None] = {}
        for rule in self.rules:
            seen.setdefault(rule.role, None)
        return tuple(seen)


def _normalise(value: str) -> str:
    return value.strip().lower()


def _split_row(body: str) -> list[str]:
    return [cell.strip() for cell in body.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(cell) for cell in cells if cell)


def _extract_section(markdown: str) -> str:
    """
    Return just the `## Handoff Routing` section, or the whole document if absent.

    The section runs until the next heading at the same or a shallower level, so
    subsections under it are included.
    """
    lines = markdown.splitlines()
    start: int | None = None
    start_level = 0

    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip().lower()

        if start is None:
            if title == SECTION_HEADING:
                start = index + 1
                start_level = level
        elif level <= start_level:
            return "\n".join(lines[start:index])

    if start is None:
        return markdown
    return "\n".join(lines[start:])


def _parse_rule(line: str) -> RoutingRule | None:
    """Parse one line into a rule, or None when the line is not a usable table row."""
    row = _TABLE_ROW_RE.match(line)
    if not row:
        return None

    cells = _split_row(row.group("body"))
    if len(cells) < 2 or _is_separator_row(cells):
        return None

    role, target = _normalise(cells[0]), _normalise(cells[1])
    if not role or not target or role == "role":  # "role" is the header; mirrors PowerShell
        return None

    when_sender = _normalise(cells[2]) if len(cells) > 2 and cells[2].strip() else None
    return RoutingRule(role=role, target=target, when_sender=when_sender)


def parse_routing_table(markdown: str) -> RoutingTable:
    """
    Parse routing rules out of workflow.md content.

    Raises ValueError when two rules would compete for the same (role, when_sender) pair,
    since silently picking one would misroute an entire swarm cycle.
    """
    rules: list[RoutingRule] = []
    seen: dict[tuple[str, str | None], str] = {}

    for line in _extract_section(markdown).splitlines():
        rule = _parse_rule(line)
        if rule is None:
            continue

        key = (rule.role, rule.when_sender)
        if key in seen:
            condition = (
                f" when sender is {rule.when_sender!r}" if rule.when_sender else " (default)"
            )
            raise ValueError(
                f"duplicate routing rule for {rule.role!r}{condition}: "
                f"already routed to {seen[key]!r}, redefined as {rule.target!r}"
            )
        seen[key] = rule.target
        rules.append(rule)

    return RoutingTable(rules=tuple(rules))


def load_routing_table(path: str | Path) -> RoutingTable:
    """
    Parse the routing table from a workflow.md path.

    A missing file yields an empty table rather than an error, matching
    Read-HandoffRoutingTable's behaviour of returning an empty hashtable.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return RoutingTable()
    return parse_routing_table(file_path.read_text(encoding="utf-8"))
