r"""Compatibility bridge to :mod:\`scheduler.queue_queries\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.queue_queries")
sys.modules[__name__] = _implementation
