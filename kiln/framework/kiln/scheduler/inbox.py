r"""Compatibility bridge to :mod:\`scheduler.inbox\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.inbox")
sys.modules[__name__] = _implementation
