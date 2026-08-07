"""
Kiln scheduler — deterministic replacement for the auto-mode wrapper LLM session.

Import as a package with `kiln/framework` on sys.path:

    from scheduler import db, routing, status_contract

Modules here are split along the line constitution/engineering.md draws between
"testable" and "environmentally unsuitable": everything in this package is either
pure (routing, status_contract) or takes its I/O target as an explicit argument
(db), so nothing needs a mock to test.
"""
