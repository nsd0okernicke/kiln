r"""Compatibility bridge to :mod:\`scheduler.pane_status\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.pane_status")
sys.modules[__name__] = _implementation
