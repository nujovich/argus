"""Argus — Hermes plugin entrypoint.

Wires the pre_tool_call hooks (CLAUDE.md §2, §6); the hooks live in their own
modules. This module is just the registration shim Hermes calls.

Two complementary, non-overlapping pre_tool_call gates are registered:
  - enforcement.on_pre_tool_call — the canonical cash gate for REAL Stripe spend,
    matching `terminal` + spend command patterns (corrected §4 ground truth:
    Stripe runs through the terminal tool, not `stripe_*` tools).
  - hook.on_pre_tool_call — the cooperative `argus_request_spend` declaration
    tool + `stripe_*` tool-name backstop (defense in depth). Matches different
    tools than enforcement, so the two never double-gate the same call.
"""

from __future__ import annotations

from . import capture
from . import enforcement
from . import hook


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", enforcement.on_pre_tool_call)
    ctx.register_hook("pre_tool_call", hook.on_pre_tool_call)
    # Capture's post_tool_call recorder — closes the intent→confirmation loop by
    # writing the REAL confirmed spend after a matched terminal command runs.
    capture.register(ctx)
