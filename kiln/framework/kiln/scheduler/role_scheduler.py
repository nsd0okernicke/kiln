"""Namespaced executable wrapper for ``scheduler.role_scheduler``."""

from scheduler.role_scheduler import *  # noqa: F403
from scheduler.role_scheduler import main

if __name__ == "__main__":
    raise SystemExit(main())
