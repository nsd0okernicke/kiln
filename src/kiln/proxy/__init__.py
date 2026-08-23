"""
The local traffic proxy — issue #6 Phase B.

Phase A measures *how many* tokens each role burns, read straight from the backend CLI's
own event stream. This package answers the other half: *what was in them*. Only the request
body shows system-prompt composition, how much the constitution merge costs, which skills
got injected and at what size, and how much context is re-sent every turn — none of which
is recoverable from a usage count.

Two modules, split on testability:

  * `capture` — the store, the redaction rules and the body-size policy. Pure functions
    plus SQLite; every decision about what is kept and what is dropped lives here and is
    unit-tested without a socket.
  * `server`  — the forwarding HTTP server. The only part that touches the network.

**Transport is a base-URL override, not TLS interception.** The agent CLI is pointed at
this proxy through its own base-URL environment variable and the proxy forwards upstream,
so there is no generated CA, nothing added to a system trust store, and nothing that breaks
when a CLI pins certificates.
"""

from __future__ import annotations

from .capture import (
    SENSITIVE_HEADERS,
    CaptureMode,
    TrafficRecord,
    TrafficStore,
    redact_headers,
)

__all__ = [
    "SENSITIVE_HEADERS",
    "CaptureMode",
    "TrafficRecord",
    "TrafficStore",
    "redact_headers",
]
