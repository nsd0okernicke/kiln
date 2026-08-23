r"""Compatibility bridge to :mod:\`scheduler.db\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.db")
sys.modules[__name__] = _implementation
