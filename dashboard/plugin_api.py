"""Argus dashboard plugin — backend API routes.

Mounted at /api/plugins/argus/ by Hermes. Reads from the ledger and writes
approval decisions / Stripe webhooks. No long-running work — the
synchronous-hold lives in the agent's pre_tool_call hook (see hook.py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


# The plugin module dir isn't necessarily on sys.path when plugin_api.py is
# imported on its own (e.g. under pytest). Inject the parent dir so flat
# imports of db / config / policy work in both contexts.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import db  # noqa: E402
import config as _cfg  # noqa: E402


router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict:
    return {
        "plugin": "argus",
        "version": "0.0.2",
        "status": "ok",
        "db": str(_cfg.db_path()),
        "telemetry_attached": _cfg.telemetry_db_path().exists(),
    }


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------


@router.get("/pnl")
async def pnl() -> dict:
    rows = db.get_pnl_per_job()
    total = {
        "revenue": sum(r["revenue"] for r in rows),
        "llm_cost": sum(r["llm_cost"] for r in rows),
        "external_spend": sum(r["external_spend"] for r in rows),
        "pnl": sum(r["pnl"] for r in rows),
    }
    return {"jobs": rows, "total": total}


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@router.get("/approvals")
async def approvals(status: Optional[str] = None) -> dict:
    if status == "pending":
        return {"items": db.get_pending_approvals()}
    return {"items": db.get_recent_approvals(limit=100)}


class DecideBody(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    actor: str = Field(default="human:dashboard")
    reason: Optional[str] = None


@router.post("/approvals/{req_id}/decide")
async def decide(req_id: str, body: DecideBody) -> dict:
    mapped = "approved" if body.decision == "approve" else "rejected"
    ok = db.decide_approval(req_id, decision=mapped, actor=body.actor, reason=body.reason)
    if not ok:
        raise HTTPException(status_code=409, detail="approval not pending or unknown")
    db.log_audit(
        body.actor,
        f"approval_{body.decision}d",
        {"approval_id": req_id, "reason": body.reason},
    )
    return {"id": req_id, "status": mapped}


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@router.get("/audit")
async def audit(limit: int = 50) -> dict:
    return {"items": db.get_recent_audit(limit=max(1, min(500, limit)))}


# ---------------------------------------------------------------------------
# Stripe webhook (TEST mode only — no signature verification in Phase 3)
# ---------------------------------------------------------------------------


class StripeEvent(BaseModel):
    type: str
    data: dict


@router.post("/webhooks/stripe")
async def stripe_webhook(evt: StripeEvent) -> dict:
    """Minimal TEST-mode webhook. Recognised events:

    - payment_intent.succeeded → revenue row
    - charge.refunded          → negative external_spend row
    """
    payload = evt.data or {}
    job_id = str(payload.get("job_id") or payload.get("metadata", {}).get("job_id") or "unknown")
    amount = float(payload.get("amount_usd") or payload.get("amount") or 0.0)
    ref = payload.get("id") or payload.get("ref")

    if evt.type == "payment_intent.succeeded":
        row_id = db.insert_ledger_row(
            job_id=job_id, kind="revenue", amount_usd=amount, source="stripe", ref=ref
        )
        db.log_audit("stripe", "revenue_received", {"job_id": job_id, "amount_usd": amount, "ref": ref})
        return {"recorded": "revenue", "id": row_id}

    if evt.type == "charge.refunded":
        row_id = db.insert_ledger_row(
            job_id=job_id, kind="external_spend", amount_usd=-amount, source="stripe", ref=ref
        )
        db.log_audit("stripe", "refund_recorded", {"job_id": job_id, "amount_usd": amount, "ref": ref})
        return {"recorded": "refund", "id": row_id}

    db.log_audit("stripe", "webhook_ignored", {"type": evt.type})
    return {"recorded": "ignored", "type": evt.type}


# ---------------------------------------------------------------------------
# Sim endpoint — drives the demo without a Stripe round-trip
# ---------------------------------------------------------------------------


class SimSpendBody(BaseModel):
    job_id: str
    cost_center_id: str = "default"
    projected_usd: float
    ref: Optional[str] = None


@router.post("/sim/spend")
async def sim_spend(body: SimSpendBody) -> dict:
    """Drive the gating pipeline end-to-end from the dashboard, no agent
    required. Useful for the demo and for local development."""
    import hook as _hook  # local import: keep startup light

    args = {
        "job_id": body.job_id,
        "cost_center_id": body.cost_center_id,
        "projected_usd": body.projected_usd,
        "ref": body.ref,
    }
    # Run the same code path the agent would hit. This blocks if approval is
    # required — keep the call async-aware by offloading to a thread.
    import anyio

    result = await anyio.to_thread.run_sync(
        lambda: _hook.on_pre_tool_call("argus_request_spend", args, "sim", )
    )
    return {"result": result or {"action": "allow"}}
