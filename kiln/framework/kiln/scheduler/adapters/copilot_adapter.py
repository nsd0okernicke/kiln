r"""Compatibility bridge to :mod:\`scheduler.adapters.copilot_adapter\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.adapters.copilot_adapter")
sys.modules[__name__] = _implementation
