r"""Compatibility bridge to :mod:\`scheduler.policies\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.policies")
sys.modules[__name__] = _implementation
