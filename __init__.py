"""Argus — Hermes plugin entrypoint.

Wires the Capture + Enforcement hook (CLAUDE.md §2, §6). The hook itself
lives in hook.py; this module is just the registration shim Hermes calls.
"""

from __future__ import annotations

import hook  # noqa: F401  (plugin dir is on sys.path at runtime)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", hook.on_pre_tool_call)
