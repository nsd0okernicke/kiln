"""Fast dependency rules that reflect boundaries the repository already intends."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[3] / "src" / "kiln"
SCHEDULER = ROOT / "scheduler"


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
        assert not imports(SCHEDULER / "domain" / name) & forbidden, name


def test_scheduler_ports_do_not_import_concrete_adapters():
    for path in (SCHEDULER / "application" / "ports").glob("*.py"):
        assert not imports(path) & {"db", "git_ops", "sqlite3", "subprocess"}, path.name


def test_scheduler_application_does_not_import_cli_or_concrete_infrastructure():
    for name in ("process_next_message.py", "recover_interrupted_work.py"):
        path = SCHEDULER / "application" / name
        assert not imports(path) & {
            "adapters",
            "argparse",
            "db",
            "git_ops",
            "infrastructure",
            "pane_status",
            "sqlite3",
            "subprocess",
        }
        assert not called_attributes(path) & {
            "mkdir",
            "open",
            "write_text",
        }


def test_application_layout_uses_semantic_modules_and_intentional_ports():
    launcher = ROOT / "launcher"
    cockpit = ROOT / "cockpit"

    assert (SCHEDULER / "application" / "process_next_message.py").is_file()
    assert (SCHEDULER / "application" / "recover_interrupted_work.py").is_file()
    assert (SCHEDULER / "application" / "ports").is_dir()

    assert (launcher / "application" / "artifacts.py").is_file()
    assert (launcher / "infrastructure" / "networking.py").is_file()

    assert (cockpit / "application" / "ports.py").is_file()
    assert not (cockpit / "application" / "ports").exists()


def test_knowledge_is_layered_like_every_other_feature_package():
    """
    The knowledge feature shipped flat -- models, manifest, extractors, service, store and cli
    side by side -- while every other feature package carries its layers. All three concerns
    are real here, so all three folders exist.
    """
    knowledge = ROOT / "knowledge"
    for layer in ("domain", "application", "infrastructure"):
        assert (knowledge / layer).is_dir(), layer
    # The flat modules are gone rather than left behind as a second way in.
    for stale in ("models.py", "manifest.py", "extractors.py", "service.py", "store.py", "cli.py"):
        assert not (knowledge / stale).exists(), stale


def test_knowledge_domain_does_not_touch_storage_transport_or_the_filesystem():
    """
    Catalog rules and text chunking are decidable from a payload and a string. Keeping the
    filesystem out is what lets a symlink escape or an oversized section be tested without
    writing one.
    """
    for path in (ROOT / "knowledge" / "domain").glob("*.py"):
        assert not imports(path) & {"argparse", "http", "pypdf", "sqlite3", "subprocess"}, path.name
        assert not called_attributes(path) & {
            "read_bytes",
            "read_text",
            "rglob",
            "write_text",
        }, path.name


def test_knowledge_application_does_not_import_any_concrete_infrastructure():
    for path in (ROOT / "knowledge" / "application").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        concrete = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and "infrastructure" in node.module
        }
        assert not concrete, f"{path.name} imports {concrete}"
        assert not imports(path) & {"argparse", "json", "sqlite3"}, path.name


def test_knowledge_ports_do_not_import_concrete_adapters():
    ports = ROOT / "knowledge" / "application" / "ports.py"
    assert not imports(ports) & {"sqlite3", "argparse", "pypdf"}
    assert ports.is_file(), "the knowledge use cases declare their outbound needs as ports"


def test_only_knowledge_infrastructure_knows_the_database_the_cli_and_the_network():
    """
    One module owns SQLite, one owns argparse, one owns the socket.

    The network rule is the newest and the easiest to lose: url support is only testable
    without a server for as long as `urllib` stays behind the `WebFetcher` port.
    """
    infrastructure = ROOT / "knowledge" / "infrastructure"
    modules = list(infrastructure.glob("*.py"))
    assert {path.name for path in modules if "sqlite3" in imports(path)} == {"sqlite_index.py"}
    assert {path.name for path in modules if "argparse" in imports(path)} == {"cli.py"}
    assert {path.name for path in modules if "urllib" in imports(path)} == {"http_fetcher.py"}


def test_knowledge_domain_url_rules_do_not_reach_the_network():
    """`urlparse` and an HTML reducer are string work; fetching is not."""
    web = ROOT / "knowledge" / "domain" / "web.py"
    assert "urllib.parse" in web.read_text(encoding="utf-8"), "url rules live here"
    assert not imports(web) & {"urllib.request", "socket", "http"}
    assert "urlopen" not in called_attributes(web)


def test_scheduler_models_do_not_import_adapters_or_infrastructure():
    assert not imports(SCHEDULER / "domain" / "models.py") & {
        "adapters",
        "db",
        "git_ops",
        "infrastructure",
        "sqlite3",
        "subprocess",
    }


def test_queue_projections_do_not_depend_on_commands_or_application():
    path = SCHEDULER / "infrastructure" / "persistence" / "queue_queries.py"
    assert not imports(path) & {"application", "db", "infrastructure"}
    assert "commit" not in called_attributes(path)


def test_queue_commands_do_not_depend_on_application_or_concrete_adapter():
    assert not imports(SCHEDULER / "infrastructure" / "persistence" / "queue_commands.py") & {
        "application",
        "infrastructure",
    }


def test_cockpit_state_projection_does_not_depend_on_http_server():
    assert "server" not in imports(ROOT / "cockpit" / "application" / "state.py")


def test_cockpit_application_does_not_import_http_transport():
    for path in (ROOT / "cockpit" / "application").glob("*.py"):
        assert not imports(path) & {"http", "server"}, path.name


def test_cockpit_action_use_cases_do_not_import_concrete_infrastructure():
    assert not imports(ROOT / "cockpit" / "application" / "actions.py") & {
        "dashboard",
        "db",
        "infrastructure",
        "retry",
        "send",
        "stop",
    }


def test_cockpit_application_does_not_import_any_concrete_infrastructure_package():
    for path in (ROOT / "cockpit" / "application").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        concrete = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and ".infrastructure" in node.module
        }
        assert not concrete, f"{path.name} imports {concrete}"


def test_launcher_application_does_not_import_scheduler_infrastructure():
    for path in (ROOT / "launcher" / "application").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        concrete = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and ".infrastructure" in node.module
        }
        assert not concrete, f"{path.name} imports {concrete}"


def test_proxy_domain_does_not_import_transport_or_persistence():
    for path in (ROOT / "proxy" / "domain").glob("*.py"):
        assert not imports(path) & {"http", "socket", "sqlite3"}, path.name


def test_launcher_domain_does_not_import_process_or_transport_adapters():
    for path in (ROOT / "launcher" / "domain").glob("*.py"):
        assert not imports(path) & {"http", "socket", "sqlite3", "subprocess"}, path.name


def test_agent_adapters_do_not_depend_on_launcher_ui():
    for path in (SCHEDULER / "infrastructure" / "agents").glob("*_adapter.py"):
        assert "launcher" not in imports(path), path.name


def test_cli_adapters_do_not_import_other_cli_adapters():
    cli = SCHEDULER / "infrastructure" / "cli"
    for path in cli.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        sibling_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
        }
        assert not sibling_imports, f"{path.name} imports CLI sibling(s): {sibling_imports}"
