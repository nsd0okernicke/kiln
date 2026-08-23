r"""Compatibility bridge to :mod:\`scheduler.send\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.send")
sys.modules[__name__] = _implementation
