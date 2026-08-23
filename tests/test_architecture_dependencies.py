"""Fast dependency rules that reflect boundaries the repository already intends."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1] / "kiln" / "framework"


def imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def called_attributes(path: Path) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_pure_scheduler_policy_does_not_import_infrastructure():
    forbidden = {"sqlite3", "subprocess", "http", "socket", "launcher"}
    for name in (
        "routing.py",
        "handoff.py",
        "status_contract.py",
        "worker_prompt.py",
        "models.py",
        "policies.py",
    ):
        assert not imports(ROOT / "scheduler" / name) & forbidden, name


def test_scheduler_ports_do_not_import_concrete_adapters():
    assert not imports(ROOT / "scheduler" / "ports.py") & {
        "db",
        "git_ops",
        "sqlite3",
        "subprocess",
    }


def test_scheduler_application_does_not_import_cli_or_concrete_infrastructure():
    assert not imports(ROOT / "scheduler" / "application.py") & {
        "adapters",
        "argparse",
        "db",
        "git_ops",
        "infrastructure",
        "pane_status",
        "sqlite3",
        "subprocess",
    }
    assert not called_attributes(ROOT / "scheduler" / "application.py") & {
        "mkdir",
        "open",
        "write_text",
    }


def test_scheduler_models_do_not_import_adapters_or_infrastructure():
    assert not imports(ROOT / "scheduler" / "models.py") & {
        "adapters",
        "db",
        "git_ops",
        "infrastructure",
        "sqlite3",
        "subprocess",
    }


def test_queue_projections_do_not_depend_on_commands_or_application():
    path = ROOT / "scheduler" / "queue_queries.py"
    assert not imports(path) & {"application", "db", "infrastructure"}
    assert "commit" not in called_attributes(path)


def test_queue_commands_do_not_depend_on_application_or_concrete_adapter():
    assert not imports(ROOT / "scheduler" / "queue_commands.py") & {
        "application",
        "infrastructure",
    }


def test_cockpit_state_projection_does_not_depend_on_http_server():
    assert "server" not in imports(ROOT / "cockpit" / "state.py")


def test_agent_adapters_do_not_depend_on_launcher_ui():
    for path in (ROOT / "scheduler" / "adapters").glob("*_adapter.py"):
        assert "launcher" not in imports(path), path.name
