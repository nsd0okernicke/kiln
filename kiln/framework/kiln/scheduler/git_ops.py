r"""Compatibility bridge to :mod:\`scheduler.git_ops\`."""

import sys
from importlib import import_module

_implementation = import_module("scheduler.git_ops")
sys.modules[__name__] = _implementation
