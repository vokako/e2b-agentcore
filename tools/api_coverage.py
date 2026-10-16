"""Print every public method of the official e2b SDK and whether e2b_agentcore supports it.

    python tools/api_coverage.py            # markdown table on stdout

The method list is read from the INSTALLED e2b package, so when E2B adds a method it shows
up here as "missing" until the provider handles it.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import e2b  # noqa: E402
from e2b.sandbox_sync.commands.command import Commands as E2BCommands  # noqa: E402
from e2b.sandbox_sync.commands.command_handle import CommandHandle as E2BHandle  # noqa: E402
from e2b.sandbox_sync.filesystem.filesystem import Filesystem as E2BFiles  # noqa: E402

import e2b_agentcore as ac  # noqa: E402

# Supported, but behaves differently from E2B in a way callers should know.
PARTIAL = {
    "Sandbox.create": "timeout/network/allow_internet_access/lifecycle/mcp/volume_mounts accepted, no effect (per Harness)",
    "Sandbox.set_timeout": "no effect; idle timeout and max lifetime are per Harness",
    "Sandbox.get_metrics": "one live sample, not a time series",
    "Sandbox.list": "from the S3 registry; reaped sandboxes drop out on the next connect/is_running",
    "Sandbox.is_running": "probes the microVM (~0.4 s), not a control-plane lookup",
    "Sandbox.git": "official e2b Git helper over our commands (E2B marks it deprecated)",
    "Commands.run": "user defaults to root; stdin=True unsupported; background output arrives in ~1 s chunks; "
                    "AgentCore drops any single output line over ~64 KB",
    "Commands.list": "background commands started through this SDK only (as E2B: envd-started only)",
    "Filesystem.write": "base64 over commands, ~100 KB/s; gzip/use_octet_stream ignored",
    "Filesystem.read": "whole file in one call; format='stream' yields one chunk",
    "Template.build": "cpu_count/memory_mb/min_free_disk_mb ignored; tags unsupported",
    "Template.build_in_background": "same as build",
}

# Official class -> our class. Template builder methods (from_*, apt_install, copy, ...) are
# inherited from e2b.Template unchanged, so only the build/lookup methods are compared.
PAIRS = [("Sandbox", e2b.Sandbox, ac.Sandbox), ("Commands", E2BCommands, ac.Commands),
         ("CommandHandle", E2BHandle, ac.CommandHandle), ("Filesystem", E2BFiles, ac.Files)]
TEMPLATE_METHODS = ["build", "build_in_background", "get_build_status", "exists", "alias_exists",
                    "assign_tags", "remove_tags", "get_tags"]


def public(cls):
    names = set()
    for klass in cls.__mro__:
        if klass.__module__.startswith("e2b") and klass.__name__ != "ClientFactory":
            names |= {n for n in vars(klass) if not n.startswith("_")}
    return sorted(names - {"connection_config", "envd_api_url", "envd_direct_url", "traffic_access_token"})


def status(ours, name):
    attr = inspect.getattr_static(ours, name, None)
    if attr is None:
        return "missing", ""
    fn = getattr(attr, "__func__", attr)
    reason = getattr(fn, "__agentcore_unsupported__", None)
    if reason:
        return "no", reason
    if isinstance(attr, property):
        try:
            attr.fget.__code__.co_names.index("NotSupportedException")
            return "no", "see message"
        except ValueError:
            pass
    return "yes", ""


def rows():
    """Every table row as (name, status, note); status is yes / partial / no / missing.

    Used by tests/test_full.py, which requires one test per row."""
    out = []
    for label, theirs, ours in PAIRS:
        for n in public(theirs):
            out.append((f"{label}.{n}", *status(ours, n)))
    for n in TEMPLATE_METHODS:
        out.append((f"Template.{n}", *status(ac.Template, n)))
    out.append(("Template.from_* / apt_install / copy / run_cmd / set_* ...", "yes", "official e2b builder, inherited"))
    return [(name, "partial", PARTIAL[name]) if s == "yes" and name in PARTIAL else (name, s, why)
            for name, s, why in out]


def main():
    from importlib.metadata import version

    table = rows()
    print(f"e2b {version('e2b')}\n")
    print("| E2B method | AgentCore | Notes |\n|---|---|---|")
    for name, s, why in table:
        print(f"| `{name}` | {s} | {why} |")
    print(f"\n{ {k: sum(1 for r in table if r[1] == k) for k in ('yes', 'partial', 'no', 'missing')} }")


if __name__ == "__main__":
    main()
