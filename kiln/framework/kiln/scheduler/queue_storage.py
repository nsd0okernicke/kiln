r"""Compatibility bridge to :mod:\`scheduler.queue_storage\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.queue_storage")
sys.modules[__name__] = _implementation
