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

from launcher import ports
from scheduler import dashboard, db
from scheduler.dashboard import DashboardContext
from scheduler.role_scheduler import configure_logging

from . import state as state_builder
from .actions import (
    ActionContext,
    ActionError,
    chat,
    check_confirmation,
    new_task,
    retry_message,
    teardown,
)

log = logging.getLogger(__name__)

#: Where the cockpit prefers to listen. Probed upward when taken, the same way the capture
#: proxy handles its port: two projects open at once is normal, and the second one silently
#: failing to bind while its URL file still names the port would point the operator at the
#: *other* project's swarm.
DEFAULT_PORT = 8765

#: How many ports above the preferred one to try before giving up.
PORT_ATTEMPTS = 20

#: Required on every mutating request. Presence is the whole check — its value carries no
#: secret and is not one. See the module docstring for why a header is sufficient here.
GUARD_HEADER = "X-Kiln-Cockpit"

#: Set this to anything non-empty to stop the cockpit opening a browser tab on launch.
NO_BROWSER_ENV = "KILN_COCKPIT_NO_BROWSER"

#: How many recent handoffs the activity feed carries in one response.
DEFAULT_ACTIVITY_LIMIT = 12

#: Grace period between answering a teardown request and carrying it out. `stop_all` kills
#: this process, so without a gap the operator's browser would see a dropped connection
#: instead of a confirmation and could not tell a teardown from a crash.
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
        awaiting_human=db.pending_for_role(
            config.dashboard.db_path, ctx.branch, ctx.human_role
        ),
        activity_limit=config.activity_limit,
    )


class CockpitHandler(BaseHTTPRequestHandler):
    """Routes. `config` is injected by `serve` onto a subclass of this."""

    config: CockpitConfig
    server_version = "KilnCockpit/1.0"

    def log_message(self, format: str, *args) -> None:  # stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)

    # --- routing -------------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            return self._send_page()
        if path == "/api/state":
            return self._guarded(lambda: self._send_json(200, gather_state(self.config)))
        if path.startswith("/api/messages/"):
            return self._guarded(lambda: self._send_message(path.rsplit("/", 1)[-1]))
        if path.startswith("/api/status/"):
            return self._guarded(lambda: self._send_status(path.rsplit("/", 1)[-1]))
        self._send_json(404, {"error": f"no route for {path}"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # Drained before any decision, including the ones that refuse. An HTTP reply sent
        # while unread request bytes are still in the socket makes Windows reset the
        # connection, so the browser sees a dropped request instead of the 403 or 404
        # explaining what it did wrong (caught by the guard-header test on Windows).
        raw = self._read_body()

        if self.headers.get(GUARD_HEADER) is None:
            # Not authentication — see the module docstring. It refuses the one shape of
            # request a hostile page can make against loopback without asking permission.
            return self._send_json(403, {"error": f"missing {GUARD_HEADER} header"})

        routes: dict[str, Callable[[dict], dict]] = {
            "/api/tasks": self._task,
            "/api/chat": self._chat,
            "/api/teardown": self._teardown,
        }
        handler = routes.get(path)
        if handler is None and path.startswith("/api/retry/"):
            message_id = path.rsplit("/", 1)[-1]
            handler = lambda body: self._retry(message_id, body)  # noqa: E731
        if handler is None:
            return self._send_json(404, {"error": f"no route for {path}"})

        try:
            body = parse_json_body(raw)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        self._guarded(lambda: self._send_json(200, handler(body)))

    # --- handlers ------------------------------------------------------------------

    def _task(self, body: dict) -> dict:
        return new_task(
            self.config.actions,
            summary=str(body.get("summary") or body.get("body") or ""),
            name=str(body.get("name") or ""),
        )

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

    def _teardown(self, body: dict) -> dict:
        """
        Answer first, then stop everything — including this process.

        `teardown` matches `cockpit.server` in `stop.KILN_PROCESS_MARKERS`, so calling it
        inline would kill the server mid-reply and the browser would see a dropped
        connection: indistinguishable from a crash, at the exact moment the operator most
        needs to know what happened.
        """
        confirm = str(body.get("confirm") or "")
        check_confirmation(confirm)
        timer = threading.Timer(
            TEARDOWN_DELAY_SEC, teardown, args=(self.config.actions,),
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
        known = {session.role for session in dashboard.read_sessions(
            self.config.dashboard.sessions_file
        )}
        if role not in known:
            return self._send_json(404, {"error": f"{role!r} is not a role in this swarm"})
        status = dashboard.read_status(self.config.status_dir, role)
        if status is None:
            return self._send_json(404, {"error": f"{role} has not reported a status yet"})
        self._send_json(200, status)

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
            code, "application/json; charset=utf-8",
            json.dumps(payload, default=str).encode("utf-8"),
        )

    def _send_bytes(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # No caching: every response is live swarm state, and a browser reusing a stored
        # `/api/state` is a cockpit showing a run that has since moved on.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def find_free_port(preferred: int = DEFAULT_PORT, attempts: int = PORT_ATTEMPTS) -> int:
    """
    The first bindable port at or above `preferred`.

    Raises rather than falling back to an ephemeral port: the URL is written to a file and
    printed in a pane, and a cockpit that landed somewhere unpredictable is harder to find
    than one that refused to start. The probe is `launcher.ports`' -- the capture proxy needs
    the identical one -- while the message stays here, since it is about cockpits.
    """
    port = ports.first_free_port(preferred, attempts)
    if port is None:
        raise CockpitError(
            f"no free port for the cockpit in {preferred}-{preferred + attempts - 1}. "
            "`kiln --stop` clears every Kiln process, including a leftover cockpit."
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
        "--lanes", default="",
        help=(
            "comma-separated roles that get a swimlane, in board order. Omitted lanes are "
            "inferred from traffic, which cannot show a role nothing has reached yet."
        ),
    )
    parser.add_argument("--human-role", default="human-in-the-loop")
    parser.add_argument(
        "--intake-role", default="",
        help="where New Task sends; the routing target of --human-role",
    )
    parser.add_argument("--activity-limit", type=int, default=DEFAULT_ACTIVITY_LIMIT)
    parser.add_argument("--url-file", default=None)
    parser.add_argument("--pid-file", default=None)
    parser.add_argument("--traffic-db", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument(
        "--no-browser", action="store_true",
        help=f"do not open a browser tab (or set {NO_BROWSER_ENV})",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> CockpitConfig:
    """Assemble the three contexts from parsed flags. No I/O beyond path building."""
    db_path = Path(args.db_path)
    sessions_file = Path(args.sessions_file)
    return CockpitConfig(
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
    print("This pane is the server. Closing it closes the cockpit.", flush=True)
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
