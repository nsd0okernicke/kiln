"""
The local web cockpit — a browser place to *operate* the swarm (issue #22).

An addition, never a replacement. `scheduler.dashboard` keeps its TTY tab and stays the
answer for SSH, headless and no-browser setups; this package answers the different question
of starting work, watching a card move between roles, handling escalations and stopping the
swarm without living in a terminal multiplexer.

Three modules, split by what they are allowed to touch:

* `state`   — pure builders. Given one gathered snapshot, produce the JSON the page renders.
* `actions` — the write half, delegating to `scheduler.send`, `scheduler.retry` and
              `kiln.launcher.stop` rather than reimplementing any of them.
* `server`  — the only module that owns a socket. Binds 127.0.0.1 and nothing else.

There is no second board filesystem and no parallel task store: every number on the page
comes from `.kiln/messages.db`, `.kiln/status/<role>.json` and `.kiln/sessions`, which is
what makes the cockpit and the terminal dashboard incapable of disagreeing.
"""
