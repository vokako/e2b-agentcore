"""AgentCore provider for the official E2B Python SDK.

E2B's SDK has no provider/plugin interface: `e2b.Sandbox` always talks to E2B's cloud (or a
self-hosted E2B via E2B_DOMAIN). This package plugs in at the Python level instead, in one
of two ways:

1. Change the import (explicit, recommended):
       from e2b_agentcore import Sandbox, Template      # instead of: from e2b import ...

2. Switch an existing code base without touching it:
       import e2b_agentcore; e2b_agentcore.install()     # once, before `from e2b import ...`
       from e2b import Sandbox, Template                 # now the AgentCore versions

Everything else (data classes, exceptions, the Template builder DSL, wait_for_port, ...)
IS the official e2b package, re-exported here, so return values and `except` clauses match.
Run `python tools/api_coverage.py` for the method-by-method support table.
"""
import e2b as _e2b
from e2b import *  # noqa: F401,F403  re-export the official types, exceptions and helpers

from ._config import Config, ensure_infra
from ._errors import NotSupportedException
from .sandbox import CommandHandle, Commands, Files, Sandbox, SandboxPaginator
from .template import Template

_ORIGINAL = {"Sandbox": _e2b.Sandbox, "Template": _e2b.Template}


def install() -> None:
    """Make `e2b.Sandbox` / `e2b.Template` resolve to the AgentCore provider.

    Affects names looked up AFTER this call (`from e2b import Sandbox` that runs later, or
    `e2b.Sandbox.create(...)`). A module that already did `from e2b import Sandbox` keeps the
    original class. AsyncSandbox / AsyncTemplate are not provided and stay E2B's.
    """
    _e2b.Sandbox, _e2b.Template = Sandbox, Template


def uninstall() -> None:
    """Undo install()."""
    _e2b.Sandbox, _e2b.Template = _ORIGINAL["Sandbox"], _ORIGINAL["Template"]


__all__ = [*_e2b.__all__, "Config", "ensure_infra", "NotSupportedException", "CommandHandle", "Commands",
           "Files", "SandboxPaginator", "install", "uninstall"]
