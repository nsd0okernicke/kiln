from hypothesis import given
from hypothesis import strategies as st

from kiln.scheduler.domain.routing import (
    RoutingRule,
    RoutingTable,
    format_routing_rules,
    parse_routing_arguments,
    parse_routing_table,
    render_routing_table,
)

identifier = st.from_regex(r"[a-z][a-z0-9-]{0,20}", fullmatch=True)
routes = st.dictionaries(identifier, identifier, max_size=12)


@given(mapping=routes)
def test_markdown_routing_round_trip(mapping: dict[str, str]) -> None:
    table = RoutingTable(tuple(RoutingRule(role, target) for role, target in mapping.items()))
    assert parse_routing_table(render_routing_table(table)) == table


@given(mapping=routes)
def test_command_line_routing_round_trip(mapping: dict[str, str]) -> None:
    table = RoutingTable(tuple(RoutingRule(role, target) for role, target in mapping.items()))
    assert parse_routing_arguments(format_routing_rules(table)) == table


@given(role=identifier, default=identifier, sender=identifier, conditional=identifier)
def test_sender_specific_route_always_precedes_default(
    role: str, default: str, sender: str, conditional: str
) -> None:
    table = RoutingTable(
        (
            RoutingRule(role, default),
            RoutingRule(role, conditional, when_sender=sender),
        )
    )
    assert table.resolve(role, sender) == conditional
    assert table.resolve(f" {role.upper()} ", f" {sender.upper()} ") == conditional
    assert table.resolve(role, "someone-else") == default
