"""Namespaced executable wrapper for ``scheduler.dashboard``."""

from scheduler.dashboard import *  # noqa: F403
from scheduler.dashboard import main

if __name__ == "__main__":
    raise SystemExit(main())
