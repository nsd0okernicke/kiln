r"""Compatibility bridge to :mod:\`scheduler.worker_prompt\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.worker_prompt")
sys.modules[__name__] = _implementation
