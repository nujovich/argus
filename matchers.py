"""Shared spend-command matchers — the single definition of "what is a spend".

GROUND TRUTH (CLAUDE.md §4, corrected): the Stripe skills do NOT register their
own tools. Every command runs through the `terminal` tool, so the hook payload is
{tool_name:"terminal", tool_input:{command:"..."}}. Spend is therefore matched on
the terminal COMMAND, not on `stripe_*` tool names.

Both the gate (enforcement) and the recorder (capture) import THIS module so they
can never disagree about what a spend is — a drift would be a money bug.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Gate ONLY real spend. Patterns are deliberately specific: over-matching blocks
# the agent on reads; under-matching leaks spend.
SPEND_PATTERNS = (
    re.compile(r"\bstripe\s+projects\s+add\b"),       # provisioning spend
    re.compile(r"\bstripe\s+projects\s+upgrade\b"),   # tier change = spend
    re.compile(r"\bmpp\s+pay\b"),                      # link-cli / 402 pay path
)

# Explicitly NOT gated — reads / no-ops that must always pass through untouched.
# (Documented for clarity + tested; SPEND_PATTERNS already exclude these.)
PASSTHROUGH_PATTERNS = (
    re.compile(r"\bstripe\s+projects\s+(?:list|catalog|status|init)\b"),
    re.compile(r"\bauth\s+status\b"),
)


def command_of(tool_name: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the shell command string iff this is a `terminal` tool call."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    cmd = args.get("command")
    if cmd is None:
        cmd = args.get("cmd")  # tolerate alternate key
    return str(cmd) if cmd is not None else None


def is_spend_command(command: str) -> bool:
    return any(p.search(command) for p in SPEND_PATTERNS)


def is_passthrough(command: str) -> bool:
    """True for the known read/no-op commands. Informational — a spend command is
    decided solely by is_spend_command; this just documents the safe set."""
    return any(p.search(command) for p in PASSTHROUGH_PATTERNS)
