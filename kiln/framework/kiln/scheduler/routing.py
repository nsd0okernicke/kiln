r"""Compatibility bridge to :mod:\`scheduler.routing\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.routing")
sys.modules[__name__] = _implementation
