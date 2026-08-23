r"""Compatibility bridge to :mod:\`scheduler.retry\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.retry")
sys.modules[__name__] = _implementation
