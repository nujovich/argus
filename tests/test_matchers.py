"""Shared spend-matcher tests — the gate and the recorder must agree.

CHANGE 1 (CLAUDE.md §2 Capture / §4 ground truth): the spend-command patterns
live in ONE module (matchers.py). Enforcement (the gate) and Capture (the
recorder) both import it, so they can never disagree about what a spend is — a
drift would be a money bug. These tests assert that single-source-of-truth.
"""

from __future__ import annotations

import pytest

import capture
import enforcement
import matchers


# A spend command is a spend command for EVERY layer. If any layer answered
# differently for any of these, the assertion below would catch the drift.
_COMMANDS = [
    "stripe projects add openai/gpt-4o",
    "stripe projects upgrade openai",
    "stripe-link-cli mpp pay --amount 12.50",
    "npx @stripe/link-cli foo mpp pay $7.00",
    "stripe projects list",
    "stripe projects catalog",
    "stripe projects status",
    "stripe projects init",
    "stripe auth status",
    "ls -la",
    "echo hello world",
    "git commit -m 'mpp'",
]


@pytest.mark.parametrize("command", _COMMANDS)
def test_gate_and_recorder_agree(command):
    # Enforcement and Capture must classify identically — they share the module.
    assert enforcement.is_spend_command(command) == matchers.is_spend_command(command)
    assert capture.is_spend_command(command) == matchers.is_spend_command(command)


def test_enforcement_does_not_redefine_the_matcher():
    # Single source of truth: both layers expose the SAME function object, not a
    # private copy that could drift from the shared definition.
    assert enforcement.is_spend_command is matchers.is_spend_command
    assert capture.is_spend_command is matchers.is_spend_command
    assert enforcement._command_of is matchers.command_of


@pytest.mark.parametrize("command", [
    "stripe projects add openai/gpt-4o",
    "stripe projects upgrade openai",
    "mpp pay --amount 12.50",
])
def test_spend_patterns_match(command):
    assert matchers.is_spend_command(command)


@pytest.mark.parametrize("command", [
    "stripe projects list",
    "stripe projects catalog",
    "stripe projects status",
    "stripe projects init",
    "stripe auth status",
])
def test_passthrough_never_gated(command):
    assert not matchers.is_spend_command(command)
    assert matchers.is_passthrough(command)
