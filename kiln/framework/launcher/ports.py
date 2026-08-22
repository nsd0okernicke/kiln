"""
Finding a free loopback port — shared by the capture proxy and the web cockpit.

Both had their own copy of this probe, which is two places to get one subtlety wrong. The
subtlety is that the answer is advisory: the port is released the moment the probe socket
closes, so something else can take it before the real listener binds. That race is accepted
by both callers because the alternative — holding the socket and handing it over — buys
nothing here: the processes that would race for it are Kiln's own, launched seconds apart,
and a genuine collision surfaces immediately as a failed bind in a pane rather than silently.

The error is deliberately *not* raised here. `cli` and `cockpit.server` fail differently and
say different things to the operator ("launch with --no-proxy" versus "`kiln --stop` clears a
leftover cockpit"), and a shared exception type would make one of those messages wrong.
"""

from __future__ import annotations

import socket

#: How many ports above the preferred one to try, when a caller does not say.
DEFAULT_ATTEMPTS = 20


def first_free_port(
    preferred: int, attempts: int = DEFAULT_ATTEMPTS, host: str = "127.0.0.1"
) -> int | None:
    """
    The first bindable port at or above `preferred`, or None when the range is exhausted.

    Probing upward rather than falling back to an ephemeral port: two projects open at once
    is normal, and a listener that landed somewhere unpredictable is harder to find than one
    that refused to start. None lets the caller say so in its own words.
    """
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    return None
