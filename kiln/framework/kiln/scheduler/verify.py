r"""Compatibility bridge to :mod:\`scheduler.verify\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.verify")
sys.modules[__name__] = _implementation
