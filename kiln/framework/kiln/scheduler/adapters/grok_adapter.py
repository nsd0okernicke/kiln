r"""Compatibility bridge to :mod:\`scheduler.adapters.grok_adapter\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.adapters.grok_adapter")
sys.modules[__name__] = _implementation
