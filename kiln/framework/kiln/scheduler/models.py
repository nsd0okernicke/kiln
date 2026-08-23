r"""Compatibility bridge to :mod:\`scheduler.models\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.models")
sys.modules[__name__] = _implementation
