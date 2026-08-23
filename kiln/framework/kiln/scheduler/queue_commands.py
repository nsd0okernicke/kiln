r"""Compatibility bridge to :mod:\`scheduler.queue_commands\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.queue_commands")
sys.modules[__name__] = _implementation
