r"""Compatibility bridge to :mod:\`scheduler.ports\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.ports")
sys.modules[__name__] = _implementation
