r"""Compatibility bridge to :mod:\`scheduler.handoff\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.handoff")
sys.modules[__name__] = _implementation
