"""Argus dashboard plugin — backend API routes.

Mounted at ``/api/plugins/argus/`` by the Hermes dashboard plugin system.
Scaffold only: a single health route that proves the front-end ↔ back-end
wiring. Real routes (P&L, approval queue, decide) land in Phase 3.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {
        "plugin": "argus",
        "version": "0.0.1",
        "status": "scaffold_ok",
    }
