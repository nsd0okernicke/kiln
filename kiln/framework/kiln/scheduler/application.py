r"""Compatibility bridge to :mod:\`scheduler.application\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.application")
sys.modules[__name__] = _implementation
