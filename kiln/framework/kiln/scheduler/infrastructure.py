r"""Compatibility bridge to :mod:\`scheduler.infrastructure\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.infrastructure")
sys.modules[__name__] = _implementation
