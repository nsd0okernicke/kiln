r"""Namespaced executable wrapper for ``scheduler.status_contract``."""

from scheduler.status_contract import *  # noqa: F403
from scheduler.status_contract import _main

if __name__ == "__main__":
    raise SystemExit(_main())
