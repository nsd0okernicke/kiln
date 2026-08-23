"""
Regression guard for the channel.py -> scheduler/db.py extraction.

These assert the MCP tools' externally visible contract — the exact response dicts agents
branch on, and the DB side effects — so "behaviour-preserving" is verified rather than
asserted.

The MCP SDK itself is stubbed. `@mcp.tool()` is transport registration, not logic, and
the installed SDK version varies by environment (see test_env_dependencies below), so
binding these tests to a particular FastMCP release would test the wrong thing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from kiln.scheduler.infrastructure.persistence import db

CHANNEL_PY = (
    Path(__file__).resolve().parents[1] / "src" / "kiln" / "mcp_server" / "channel.py"
)

pytestmark = pytest.mark.integration


class _StubFastMCP:
    """Minimal stand-in for FastMCP: `tool()` registers and returns the function as-is."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.registered: list[str] = []

    def tool(self):
        def decorator(fn):
            self.registered.append(fn.__name__)
            return fn

        return decorator

    def run(self):  # pragma: no cover - never called in tests
        raise AssertionError("mcp.run() must not be invoked by tests")


@pytest.fixture
def load_channel(monkeypatch):
    """Import channel.py fresh with a stubbed MCP SDK and controlled env."""

    def _load(**env):
        stub = types.ModuleType("mcp.server.fastmcp")
        stub.FastMCP = _StubFastMCP
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", stub)

        for key in ("KILN_ROLE", "KILN_DB_PATH", "KILN_BRANCH", "KILN_POLL_INTERVAL",
                    "KILN_CHANNEL_LOG"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))

        spec = importlib.util.spec_from_file_location("kiln_channel_under_test", CHANNEL_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return _load


@pytest.fixture
def channel(load_channel, db_path):
    return load_channel(
        KILN_ROLE="coder",
        KILN_DB_PATH=str(db_path),
        KILN_BRANCH="main",
        KILN_POLL_INTERVAL="0.01",
    )


class TestStartupContract:
    @pytest.mark.parametrize(
        "env",
        [{}, {"KILN_ROLE": "coder"}, {"KILN_DB_PATH": "x.db"}],
        ids=["neither", "role-only", "db-only"],
    )
    def test_missing_required_env_exits(self, load_channel, env):
        with pytest.raises(SystemExit) as excinfo:
            load_channel(**env)
        assert excinfo.value.code == 1

    def test_registers_the_expected_tools(self, channel):
        assert set(channel.mcp.registered) == {
            "wait_for_message",
            "get_channel_status",
            "mark_processing",
            "mark_processed",
        }

    def test_branch_defaults_to_main(self, load_channel, db_path):
        module = load_channel(KILN_ROLE="coder", KILN_DB_PATH=str(db_path))
        assert module.BRANCH == "main"


class TestGetChannelStatus:
    def test_reports_configuration_and_depth(self, channel, db_path, add_message):
        add_message(target="coder")
        add_message(target="coder")
        add_message(target="refactorer")

        status = channel.get_channel_status()

        assert status["role"] == "coder"
        assert status["branch"] == "main"
        assert status["db_path"] == str(db_path)
        assert status["queued_messages"] == 2
        assert status["status"] == "running"

    def test_reports_error_instead_of_raising(self, load_channel, tmp_path):
        unreachable = tmp_path / "nonexistent" / "x.db"
        module = load_channel(KILN_ROLE="coder", KILN_DB_PATH=str(unreachable))
        status = module.get_channel_status()
        assert status["status"] == "error"
        assert status["error"]


class TestMarkProcessing:
    def test_success_response_and_side_effect(self, channel, add_message, read_message):
        message_id = add_message(target="coder")
        assert channel.mark_processing(message_id) == {
            "success": True,
            "message_id": message_id,
            "status": "processing",
        }
        assert read_message(message_id)["status"] == db.STATUS_PROCESSING

    def test_unknown_id_reports_failure(self, channel):
        result = channel.mark_processing("missing-id")
        assert result["success"] is False
        assert result["error"] == "message id missing-id not found"


class TestMarkProcessed:
    def test_success_response_and_side_effect(self, channel, add_message, read_message):
        message_id = add_message(target="coder")
        assert channel.mark_processed(message_id) == {
            "success": True,
            "message_id": message_id,
            "status": "processed",
        }
        stored = read_message(message_id)
        assert stored["status"] == db.STATUS_PROCESSED
        assert stored["processed_at"]

    def test_unknown_id_reports_failure(self, channel):
        result = channel.mark_processed("missing-id")
        assert result["success"] is False
        assert result["error"] == "message id missing-id not found"


class TestWaitForMessage:
    def test_returns_queued_message_and_marks_delivered(self, channel, add_message, read_message):
        message_id = add_message(target="coder", content="do the thing")

        result = asyncio.run(asyncio.wait_for(channel.wait_for_message(), timeout=5))

        assert result["received"] is True
        assert result["id"] == message_id
        assert result["content"] == "do the thing"
        assert result["sender"] == "specifier"
        assert read_message(message_id)["status"] == db.STATUS_DELIVERED

    def test_blocks_while_the_inbox_is_empty(self, channel):
        async def _run():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(channel.wait_for_message(), timeout=0.2)

        asyncio.run(_run())

    def test_ignores_messages_for_other_roles(self, channel, add_message):
        add_message(target="refactorer")

        async def _run():
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(channel.wait_for_message(), timeout=0.2)

        asyncio.run(_run())

    def test_picks_up_a_message_that_arrives_mid_poll(self, channel, db_path):
        async def _run():
            task = asyncio.create_task(channel.wait_for_message())
            await asyncio.sleep(0.05)
            assert not task.done()
            db.insert_handoff(db_path, "specifier", "coder", "late arrival", "main")
            return await asyncio.wait_for(task, timeout=5)

        assert asyncio.run(_run())["content"] == "late arrival"

    def test_respects_priority_order(self, channel, add_message):
        add_message(target="coder", priority=50, content="normal")
        add_message(target="coder", priority=1, content="urgent")
        result = asyncio.run(asyncio.wait_for(channel.wait_for_message(), timeout=5))
        assert result["content"] == "urgent"


def test_env_dependencies_are_documented_not_assumed():
    """
    channel.py imports `mcp.server.fastmcp`, which exists in mcp 1.x but not 2.x, and
    `.mcp.json` launches it with a bare `python`. Whether that interpreter has a
    compatible `mcp` installed is an environment concern this suite deliberately does not
    depend on — it stubs the SDK instead. This test only pins the import path so a change
    to it is a conscious decision.
    """
    source = CHANNEL_PY.read_text(encoding="utf-8")
    assert "from mcp.server.fastmcp import FastMCP" in source
