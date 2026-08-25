"""
The cockpit's HTTP surface — the only module here that owns a socket.

Stdlib `http.server`, deliberately. The whole API is eight routes over data this repo
already knows how to read; a framework would add a dependency to every machine that runs
Kiln in exchange for routing sugar. `ThreadingHTTPServer` because the page polls while a
teardown or a send is in flight, and a single-threaded server would serialise them.

**Local only, and structurally so.** The bind address is 127.0.0.1 and there is no flag to
change it: this endpoint starts work, retries failed cycles and kills every Kiln process on
the machine, with no authentication of any kind. A `--host` option would be a foot-gun whose
only purpose is to remove the property that makes no-auth acceptable.

Loopback is not by itself enough, though, because a web page the operator happens to have
open can also reach 127.0.0.1. Two things close that:

* every mutating route requires the `X-Kiln-Cockpit` header, which a cross-origin form or
  image cannot set without a preflight this server never approves (no CORS headers are sent,
  so the preflight fails and the request is never made);
* teardown additionally requires its confirmation string in the body.

Polling, not SSE: the terminal dashboard has redrawn on a 2-second poll since it shipped and
nobody has wanted it faster. A push channel is Phase 3 material if polling ever proves
insufficient, and it would have to survive the same "the swarm is the thing that matters,
the view must never take it down" rule the dashboard's error handling already follows.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from kiln.launcher.infrastructure import networking
from kiln.scheduler.infrastructure.cli import dashboard
from kiln.scheduler.infrastructure.cli.dashboard import DashboardContext
from kiln.scheduler.infrastructure.persistence import db, task_store
from kiln.scheduler.infrastructure.runtime import configure_logging

from ...application import state as state_builder
from ...application import test_metrics as test_metrics_builder
from ...application.actions import (
    ActionContext,
    ActionError,
    archive_task,
    chat,
    check_confirmation,
    create_task,
    handoff_task,
    retry_message,
    send_to,
    teardown,
    update_task,
)
from .. import test_reports
from ..actions_gateway import KilnActionGateway

log = logging.getLogger(__name__)

#: Preferred port; startup probes upward when occupied.
DEFAULT_PORT = 8765

#: How many ports above the preferred one to try before giving up.
PORT_ATTEMPTS = 20

#: Presence guard on mutating requests; not an authentication secret.
GUARD_HEADER = "X-Kiln-Cockpit"

#: Set this to anything non-empty to stop the cockpit opening a browser tab on launch.
NO_BROWSER_ENV = "KILN_COCKPIT_NO_BROWSER"

#: How many recent handoffs the activity feed carries in one response.
DEFAULT_ACTIVITY_LIMIT = 12
INITIAL_LOG_BYTES = 64 * 1024

#: Lets the teardown response flush before `stop_all` kills this process.
TEARDOWN_DELAY_SEC = 0.5

STATIC_DIR = Path(__file__).resolve().parent / "static"


class CockpitError(Exception):
    """Fatal startup failure, e.g. no bindable port."""


def parse_json_body(raw: bytes) -> dict:
    """
    A request body as a JSON object, or ValueError saying why not.

    A pure function of the bytes, separate from reading them, because the two happen at
    different points: the body must be off the socket before any reply is written (see
    `do_POST`), while parsing it only matters once the request has been accepted.
    """
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"body is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


@dataclass(frozen=True)
class CockpitConfig:
    """Everything the handler needs, kept off the handler class itself."""

    dashboard: DashboardContext
    cockpit: state_builder.CockpitContext
    actions: ActionContext
    activity_limit: int = DEFAULT_ACTIVITY_LIMIT
    #: Where relative report paths resolve from. The project root, not the cockpit's cwd.
    project_root: Path = Path()
    #: Where the report configuration lives. The *path*, not the parsed config: it is re-read
    #: on each poll so that creating or editing the file takes effect without restarting the
    #: cockpit. Loading it once at startup meant a config written a minute after launch was
    #: invisible until the whole swarm came down, which reads as the feature being broken.
    test_metrics_path: Path = Path()

    @property
    def status_dir(self) -> Path:
        return self.dashboard.status_dir


def gather_state(config: CockpitConfig) -> dict:
    """
    Read every source once and assemble the `/api/state` document.

    The impure half of `state.py`: this is where the database and the filesystem are touched,
    so the builders themselves stay testable without either. Named `gather_` rather than
    `build_` precisely to keep that line visible -- `state.build_state` is the pure one, and
    two functions with one name would hide which of them does I/O.
    """
    snapshot = dashboard.collect(config.dashboard)
    ctx = config.cockpit
    work_items = db.work_item_messages(config.dashboard.db_path, ctx.branch)
    return state_builder.build_state(
        ctx,
        snapshot,
        work_items=work_items,
        cycles=db.cycles_by_work_item(config.dashboard.db_path, ctx.branch),
        failed=db.failed_messages(config.dashboard.db_path, ctx.branch),
        awaiting_human=db.pending_for_role(config.dashboard.db_path, ctx.branch, ctx.human_role),
        activity_limit=config.activity_limit,
        tasks=task_store.list_tasks(config.dashboard.db_path, branch=ctx.branch),
    )


def gather_test_metrics(config: CockpitConfig) -> dict:
    """
    Read the project's test reports and assemble `/api/test-metrics` (issue #27).

    Deliberately its own endpoint rather than a key inside `/api/state`. The page fetches
    both on the same tick, so the operator sees one refresh either way -- but a report that
    is missing, huge or malformed can then only spoil its own panel. Folding it into the
    state document would put an unparseable XML file on the path of the board, the queue and
    the attention rail.

    The configuration is read here rather than at startup, on the same poll as the reports.
    One extra small read beside three report files is not a cost worth a restart -- and the
    restart was the surprising part, because "the reports change, the configuration does not"
    is false in exactly the case that matters: setting the feature up for the first time.
    """
    try:
        metrics_config = test_reports.load_config(config.test_metrics_path)
    except test_reports.ReportError as error:
        # A broken config is not a missing one; saying "create one" would send the operator
        # to write a file that is already sitting there with a typo in it.
        return test_metrics_builder.unavailable(str(error))
    if metrics_config is None:
        # Names the path, and says who writes it. `.kiln/` is gitignored and Kiln re-adds that
        # rule on every launch, so this file arrives by hand or not at all -- an operator who
        # reads "not configured" and waits for a cycle to produce one waits forever.
        return test_metrics_builder.unavailable(
            f"no {test_reports.CONFIG_DISPLAY_PATH} — create one to show test health"
        )
    return test_reports.collect(metrics_config, root=config.project_root)


#: Exact-match GET routes, each a builder that reads every source afresh on the request.
#: A table rather than one `if` apiece: the prefix routes need the trailing identifier and
#: cannot join it, but these two were only ever growing a branch each.
DOCUMENT_ROUTES: dict[str, Callable[[CockpitConfig], dict]] = {
    "/api/state": gather_state,
    "/api/test-metrics": gather_test_metrics,
}


def _log_query(query: dict[str, list[str]]) -> tuple[str, int] | str:
    stream = (query.get("stream") or ["scheduler"])[0]
    if stream not in {"scheduler", "worker"}:
        return "stream must be scheduler or worker"
    try:
        return stream, max(0, int((query.get("after") or ["0"])[0]))
    except ValueError:
        return "after must be a non-negative integer"


def _log_start(size: int, after: int) -> tuple[int, bool]:
    if size < after:
        return 0, True
    if after == 0 and size > INITIAL_LOG_BYTES:
        return size - INITIAL_LOG_BYTES, True
    return after, False


class CockpitHandler(BaseHTTPRequestHandler):
    """Routes. `config` is injected by `serve` onto a subclass of this."""

    config: CockpitConfig
    server_version = "KilnCockpit/1.0"

    def log_message(self, format: str, *args) -> None:  # stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    # --- routing -------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        handler = self._get_handler(path, parsed.query)
        if handler is None:
            return self._send_json(404, {"error": f"no route for {path}"})
        self._guarded(handler)

    def _get_handler(self, path: str, query: str) -> Callable[[], None] | None:
        if path == "/":
            return self._send_page
        document = DOCUMENT_ROUTES.get(path)
        if document is not None:
            return lambda: self._send_json(200, document(self.config))
        identifier = unquote(path.rsplit("/", 1)[-1])
        if path.startswith("/api/messages/"):
            return lambda: self._send_message(identifier)
        if path.startswith("/api/status/"):
            return lambda: self._send_status(identifier)
        if path.startswith("/api/logs/"):
            return lambda: self._send_log(identifier, parse_qs(query))
        return None

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # Drain request bytes before replying to avoid connection resets on Windows.
        raw = self._read_body()

        if self.headers.get(GUARD_HEADER) is None:
            return self._send_json(403, {"error": f"missing {GUARD_HEADER} header"})

        handler = self._post_handler(path)
        if handler is None:
            return self._send_json(404, {"error": f"no route for {path}"})

        try:
            body = parse_json_body(raw)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        self._guarded(lambda: self._send_json(200, handler(body)))

    def _post_handler(self, path: str) -> Callable[[dict], dict] | None:
        routes: dict[str, Callable[[dict], dict]] = {
            "/api/send": self._send,
            "/api/tasks": self._task,
            "/api/chat": self._chat,
            "/api/teardown": self._teardown,
        }
        handler = routes.get(path)
        if handler is not None:
            return handler
        dynamic = (
            ("/api/retry/", lambda identifier: lambda body: self._retry(identifier, body)),
            ("/api/ack/", lambda identifier: lambda _body: self._ack(identifier)),
        )
        for prefix, factory in dynamic:
            if path.startswith(prefix):
                return factory(unquote(path.rsplit("/", 1)[-1]))
        return self._task_post_handler(path)

    def _task_post_handler(self, path: str) -> Callable[[dict], dict] | None:
        parts = path.split("/")
        if len(parts) < 4 or parts[:3] != ["", "api", "tasks"]:
            return None
        identifier = unquote(parts[3])
        if len(parts) == 4:
            return lambda body: self._update_task(identifier, body)
        actions = {
            "handoff": lambda body: self._handoff_task(identifier, body),
            "archive": lambda _body: self._archive_task(identifier),
        }
        return actions.get(parts[4]) if len(parts) == 5 else None

    # --- handlers ------------------------------------------------------------------

    def _send(self, body: dict) -> dict:
        """Queue a handoff for a role the operator chose. The general form of the two below."""
        return send_to(
            self.config.actions,
            target=str(body.get("target") or ""),
            summary=str(body.get("summary") or body.get("body") or ""),
            work_item=str(body.get("work_item") or ""),
        )

    def _task(self, body: dict) -> dict:
        return create_task(
            self.config.actions,
            work_item=str(body.get("work_item") or ""),
            title=str(body.get("title") or ""),
            body=str(body.get("body") or ""),
        )

    def _update_task(self, identifier: str, body: dict) -> dict:
        return update_task(
            self.config.actions,
            identifier=identifier,
            title=str(body["title"]) if "title" in body else None,
            body=str(body["body"]) if "body" in body else None,
        )

    def _handoff_task(self, identifier: str, body: dict) -> dict:
        return handoff_task(
            self.config.actions,
            identifier=identifier,
            target=str(body.get("target") or ""),
        )

    def _archive_task(self, identifier: str) -> dict:
        return archive_task(self.config.actions, identifier=identifier)

    def _chat(self, body: dict) -> dict:
        return chat(
            self.config.actions,
            summary=str(body.get("summary") or body.get("body") or ""),
            work_item=str(body.get("work_item") or ""),
        )

    def _retry(self, message_id: str, body: dict) -> dict:
        return retry_message(
            self.config.actions,
            message_id=message_id,
            guidance=str(body.get("guidance") or ""),
        )

    def _ack(self, message_id: str) -> dict:
        row = db.acknowledge_message(
            self.config.dashboard.db_path,
            message_id,
            self.config.cockpit.human_role,
            self.config.cockpit.branch,
        )
        if row is None:
            raise ActionError("that message is not awaiting acknowledgement")
        return {"acknowledged": True, "message_id": message_id}

    def _teardown(self, body: dict) -> dict:
        """
        Answer first, then stop everything — including this process.

        `teardown` matches this server in `stop.KILN_PROCESS_MARKERS`, so calling it
        inline would kill the server mid-reply and the browser would see a dropped
        connection: indistinguishable from a crash, at the exact moment the operator most
        needs to know what happened.
        """
        confirm = str(body.get("confirm") or "")
        check_confirmation(confirm)
        timer = threading.Timer(
            TEARDOWN_DELAY_SEC,
            teardown,
            args=(self.config.actions,),
            kwargs={"confirm": confirm},
        )
        timer.daemon = True
        timer.start()
        return {"stopping": True, "in_seconds": TEARDOWN_DELAY_SEC}

    def _send_page(self) -> None:
        page = STATIC_DIR / "cockpit.html"
        if not page.is_file():  # pragma: no cover - ships with the package
            return self._send_json(500, {"error": "cockpit.html is missing"})
        self._send_bytes(200, "text/html; charset=utf-8", page.read_bytes())

    def _send_message(self, message_id: str) -> None:
        """The full handoff body behind a card — the issue's Documents equivalent."""
        row = db.get_message(self.config.dashboard.db_path, message_id)
        if row is None:
            return self._send_json(404, {"error": f"no message {message_id}"})
        self._send_json(200, row)

    def _send_status(self, role: str) -> None:
        """
        One role's raw status JSON.

        The role is checked against the sessions file before it reaches a path, so this
        endpoint reads `.kiln/status/<role>.json` for a launched role and nothing else. A
        name straight from the URL would make `../` a file-read primitive on a server with
        no authentication.
        """
        if role not in self._known_roles():
            return self._send_json(404, {"error": f"{role!r} is not a role in this swarm"})
        status = dashboard.read_status(self.config.status_dir, role)
        if status is None:
            return self._send_json(404, {"error": f"{role} has not reported a status yet"})
        self._send_json(200, status)

    def _send_log(self, role: str, query: dict[str, list[str]]) -> None:
        """Return the newly appended bytes of one role log, bounded on first read."""
        if role not in self._known_roles():
            return self._send_json(404, {"error": f"{role!r} is not a role in this swarm"})

        parsed = _log_query(query)
        if isinstance(parsed, str):
            return self._send_json(400, {"error": parsed})
        stream, after = parsed

        path = self.config.dashboard.db_path.parent / "logs" / f"{stream}-{role}.log"
        if not path.is_file():
            return self._send_json(
                200,
                {
                    "role": role,
                    "stream": stream,
                    "offset": 0,
                    "lines": [],
                    "truncated": False,
                },
            )
        start, truncated = _log_start(path.stat().st_size, after)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read()
        return self._send_json(
            200,
            {
                "role": role,
                "stream": stream,
                "offset": start + len(raw),
                "lines": raw.decode("utf-8", errors="replace").splitlines(),
                "truncated": truncated,
            },
        )

    def _known_roles(self) -> set[str]:
        return {
            session.role for session in dashboard.read_sessions(self.config.dashboard.sessions_file)
        }

    # --- plumbing ------------------------------------------------------------------

    def _guarded(self, work: Callable[[], None]) -> None:
        """
        Run one request's work, turning any failure into a reply rather than a dead poll.

        The cockpit is a view onto a running swarm and must never be the thing that stops:
        a page that goes blank because one message had an unparseable timestamp is worse
        than one that shows the error and keeps polling. Same rule `dashboard.main` applies
        to its own render loop.
        """
        try:
            work()
        except ActionError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # the view must never take the swarm's place
            log.exception("request failed: %s %s", self.command, self.path)
            self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _read_body(self) -> bytes:
        """Consume exactly the bytes the request announced. Never raises."""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, code: int, payload: dict) -> None:
        self._send_bytes(
            code,
            "application/json; charset=utf-8",
            json.dumps(payload, default=str).encode("utf-8"),
        )

    def _send_bytes(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def find_free_port(preferred: int = DEFAULT_PORT, attempts: int = PORT_ATTEMPTS) -> int:
    """
    The first bindable port at or above `preferred`.

    Raises rather than falling back to an ephemeral port: the URL is written to a file and
    printed in a pane, and a cockpit that landed somewhere unpredictable is harder to find
    than one that refused to start. The probe is shared with the capture proxy through
    `kiln.launcher.infrastructure.networking`, while
    the identical one -- while the message stays here, since it is about cockpits.
    """
    port = networking.first_free_port(preferred, attempts)
    if port is None:
        raise CockpitError(
            f"no free port for the cockpit in {preferred}-{preferred + attempts - 1}. "
            "`kiln --stop` clears every Kiln process, including a leftover kiln.cockpit."
        )
    return port


def serve(config: CockpitConfig, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """
    Build a bound server. The caller owns `serve_forever`/`shutdown`.

    `port=0` binds an ephemeral port; read it back from `server.server_address[1]`, which is
    what the tests do. The host is not a parameter — see the module docstring.
    """
    handler = type("ConfiguredCockpitHandler", (CockpitHandler,), {"config": config})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def write_launch_files(url: str, url_file: Path | None, pid_file: Path | None) -> None:
    """
    Record where the cockpit is, for anything that needs to find it after the fact.

    The URL file is how a human in another terminal learns the port when the preferred one
    was taken; the pid file is a courtesy for the same case. Neither is load-bearing —
    `kiln --stop` finds the process by command line like every other Kiln pane — so a
    read-only `.kiln/` degrades to a warning rather than a failed launch.
    """
    for path, value in ((url_file, url), (pid_file, str(os.getpid()))):
        if path is None:
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)


def open_browser(url: str) -> bool:
    """Open the cockpit, unless the operator opted out. True when a browser was asked."""
    if os.environ.get(NO_BROWSER_ENV):
        return False
    try:
        # Threaded because `webbrowser.open` blocks on some platforms while the browser
        # starts, and the pane should print its URL immediately either way.
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return True
    except Exception:  # pragma: no cover - a headless box has no browser and that is fine
        log.debug("could not open a browser", exc_info=True)
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiln cockpit", description="Local browser cockpit for a running Kiln swarm."
    )
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--status-dir", required=True)
    parser.add_argument("--sessions-file", required=True)
    parser.add_argument("--project-name", default="")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--lanes",
        default="",
        help=(
            "comma-separated roles that get a swimlane, in board order. Omitted lanes are "
            "inferred from traffic, which cannot show a role nothing has reached yet."
        ),
    )
    parser.add_argument("--human-role", default="human-in-the-loop")
    parser.add_argument(
        "--intake-role",
        default="",
        help="default destination when a backlog task is handed off",
    )
    parser.add_argument("--activity-limit", type=int, default=DEFAULT_ACTIVITY_LIMIT)
    parser.add_argument(
        "--project-root",
        default="",
        help="root that relative test-report paths resolve from (default: cwd)",
    )
    parser.add_argument(
        "--test-metrics",
        default="",
        help=(
            f"path to the report config; omitted looks for "
            f"{test_reports.CONFIG_DISPLAY_PATH} under --project-root. "
            "Reports are read, never produced -- the cockpit does not run test commands."
        ),
    )
    parser.add_argument("--url-file", default=None)
    parser.add_argument("--pid-file", default=None)
    parser.add_argument("--traffic-db", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=f"do not open a browser tab (or set {NO_BROWSER_ENV})",
    )
    return parser


def _project_root(args: argparse.Namespace) -> Path:
    """Where relative report paths resolve from -- the launcher always passes it explicitly."""
    return Path(args.project_root) if args.project_root else Path.cwd()


def _test_metrics_path(args: argparse.Namespace, project_root: Path) -> Path:
    """An explicit `--test-metrics` wins; otherwise the one documented location."""
    return Path(args.test_metrics) if args.test_metrics else test_reports.config_path(project_root)


def config_from_args(args: argparse.Namespace) -> CockpitConfig:
    """Assemble the three contexts from parsed flags. No I/O beyond path building."""
    db_path = Path(args.db_path)
    sessions_file = Path(args.sessions_file)
    project_root = _project_root(args)
    return CockpitConfig(
        project_root=project_root,
        test_metrics_path=_test_metrics_path(args, project_root),
        dashboard=DashboardContext(
            db_path=db_path,
            branch=args.branch,
            status_dir=Path(args.status_dir),
            sessions_file=sessions_file,
            project_name=args.project_name or Path.cwd().name,
            activity_limit=args.activity_limit,
            traffic_db=Path(args.traffic_db) if args.traffic_db else None,
        ),
        cockpit=state_builder.CockpitContext(
            project_name=args.project_name or Path.cwd().name,
            branch=args.branch,
            lanes=tuple(name.strip() for name in args.lanes.split(",") if name.strip()),
            human_role=args.human_role,
            intake_role=args.intake_role,
        ),
        actions=ActionContext(
            db_path=db_path,
            branch=args.branch,
            human_role=args.human_role,
            intake_role=args.intake_role,
            sessions_file=sessions_file,
            gateway=KilnActionGateway(),
        ),
        activity_limit=args.activity_limit,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_file, label="kiln-cockpit")

    port = args.port if args.port == 0 else find_free_port(args.port)
    server = serve(config_from_args(args), port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}"

    write_launch_files(
        url,
        Path(args.url_file) if args.url_file else None,
        Path(args.pid_file) if args.pid_file else None,
    )
    print(f"Kiln cockpit: {url}", flush=True)
    print("This pane is the server. Closing it closes the kiln.cockpit.", flush=True)
    if not args.no_browser:
        open_browser(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
