"""Console and logging setup shared by command-line processes."""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path


def enable_unicode_output() -> None:
    """Configure text streams for UTF-8 without failing on nonstandard streams."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def configure_logging(log_file: str | Path | None = None, label: str = "kiln-scheduler") -> None:
    """Log to stderr and, when requested, to a persistent UTF-8 file."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(path, encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - permissions/full disk
            print(f"warning: could not open scheduler log {path}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{label}/%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
