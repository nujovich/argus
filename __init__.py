"""Argus — Hermes plugin entrypoint (Phase 2 scaffold).

The real Capture + Enforcement hooks land in Phase 3. For now ``register``
is a no-op so the plugin loads cleanly and the dashboard tab renders.

See CLAUDE.md for the full design.
"""

from __future__ import annotations


def register(ctx) -> None:  # noqa: ARG001 — ctx unused until Phase 3
    """Plugin registration hook. Intentionally a no-op in the scaffold."""
    return None
