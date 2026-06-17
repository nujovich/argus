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
        f"approval_{mapped}",
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


def _extract_stripe_fields(evt_type: str, data: dict) -> tuple[str, float, Optional[str]]:
    """Pull job_id / amount_usd / ref out of a Stripe webhook payload.

    Real Stripe webhooks nest the object under ``data.object`` and use cents
    + ``metadata`` for custom fields. The sim payloads we ship with the demo
    use a flatter shape. This function accepts both so the same endpoint
    serves curl-driven demos and ``stripe trigger`` / real webhooks.
    """
    obj = data.get("object") if isinstance(data.get("object"), dict) else data

    # job_id: prefer metadata.job_id (the convention for real Stripe), fall
    # back to a top-level job_id (sim payload), default to "unknown".
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    job_id = str(
        metadata.get("job_id")
        or obj.get("job_id")
        or data.get("job_id")
        or "unknown"
    )

    # amount: real Stripe sends cents as int; sim sends dollars as float.
    # For refunds the relevant field is amount_refunded.
    if evt_type == "charge.refunded":
        amount_field = obj.get("amount_refunded") or obj.get("amount_usd") or 0
    else:
        amount_field = (
            obj.get("amount_usd")
            or data.get("amount_usd")
            or obj.get("amount")
            or data.get("amount")
            or 0
        )
    # Stripe convention: integers are cents; floats stay dollars.
    if isinstance(amount_field, int) and amount_field >= 100:
        amount = amount_field / 100.0
    else:
        amount = float(amount_field)

    ref = obj.get("id") or data.get("id") or obj.get("ref") or data.get("ref")
    return job_id, amount, ref


@router.post("/webhooks/stripe")
async def stripe_webhook(evt: StripeEvent) -> dict:
    """Stripe webhook receiver. Accepts both real Stripe envelopes
    (``data.object`` nested, cents) and the flatter sim payloads the demo
    script uses. Recognised events:

    - ``payment_intent.succeeded`` → revenue row
    - ``charge.refunded``          → negative external_spend row
    """
    job_id, amount, ref = _extract_stripe_fields(evt.type, evt.data or {})

    if evt.type == "payment_intent.succeeded":
        row_id = db.insert_ledger_row(
            job_id=job_id, kind="revenue", amount_usd=amount, source="stripe", ref=ref
        )
        db.log_audit(
            "stripe", "revenue_received",
            {"job_id": job_id, "amount_usd": amount, "ref": ref},
        )
        return {"recorded": "revenue", "id": row_id}

    if evt.type == "charge.refunded":
        row_id = db.insert_ledger_row(
            job_id=job_id, kind="external_spend", amount_usd=-amount,
            source="stripe", ref=ref,
        )
        db.log_audit(
            "stripe", "refund_recorded",
            {"job_id": job_id, "amount_usd": amount, "ref": ref},
        )
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
    session_id: Optional[str] = None   # agent's real task_id when called from a live session


@router.post("/sim/spend")
async def sim_spend(body: SimSpendBody) -> dict:
    """Drive the gating pipeline end-to-end. Used by the deterministic
    demo driver AND by live Hermes agents that hit this endpoint via the
    terminal/HTTP tool — the agent's own task_id should be passed as
    ``session_id`` so the ledger row joins correctly against telemetry."""
    import hook as _hook  # local import: keep startup light

    args = {
        "job_id": body.job_id,
        "cost_center_id": body.cost_center_id,
        "projected_usd": body.projected_usd,
        "ref": body.ref,
    }
    task_id = body.session_id or "sim"
    import anyio

    result = await anyio.to_thread.run_sync(
        lambda: _hook.on_pre_tool_call("argus_request_spend", args, task_id)
    )
    return {"result": result or {"action": "allow"}}
