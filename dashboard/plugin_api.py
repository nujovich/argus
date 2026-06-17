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
    terminal/HTTP tool.

    On ALLOW the response carries an ``auth_token`` the agent must echo in
    its next Stripe call's ``metadata.argus_auth_token``. Without it, the
    hook's backstop layer blocks the Stripe spend (see CLAUDE.md §6)."""
    import hook as _hook

    task_id = body.session_id or "sim"
    import anyio

    result = await anyio.to_thread.run_sync(
        lambda: _hook.process_declaration_for_api(
            job_id=body.job_id,
            cost_center_id=body.cost_center_id,
            projected_usd=body.projected_usd,
            ref=body.ref,
            task_id=task_id,
        )
    )
    return {"result": result}


# ---------------------------------------------------------------------------
# Compute Allocator — Phase 4.5
# ---------------------------------------------------------------------------


class ComputeRequestBody(BaseModel):
    job_id: str
    cost_center_id: str
    expected_revenue_usd: float
    projected_burn_usd: float
    ref: Optional[str] = None
    session_id: Optional[str] = None


@router.post("/sim/compute")
async def sim_compute(body: ComputeRequestBody) -> dict:
    """Allocate a compute tier (Ultra / Base / Reject) for a job.

    Argus reads (expected_revenue, projected_burn), runs the compute
    policy, and assigns a Nemotron tier with an auth token. The agent
    must use the returned model + token for its inference calls; the
    compute-integrity inspector then validates the telemetry session
    actually ran on the authorized model. See CLAUDE.md §3.2."""
    import hook as _hook
    import anyio

    task_id = body.session_id or "sim"
    result = await anyio.to_thread.run_sync(
        lambda: _hook.process_compute_request_for_api(
            job_id=body.job_id,
            cost_center_id=body.cost_center_id,
            expected_revenue_usd=body.expected_revenue_usd,
            projected_burn_usd=body.projected_burn_usd,
            ref=body.ref,
            task_id=task_id,
        )
    )
    return {"result": result}


@router.get("/compute/allocations")
async def list_compute_allocations(job_id: Optional[str] = None) -> dict:
    """List compute allocations. Used by the dashboard's fleet view."""
    items = db.get_compute_allocations(job_id=job_id)
    return {"items": items}


class LlmCostBody(BaseModel):
    job_id: str
    amount_usd: float
    ref: Optional[str] = None
    session_id: Optional[str] = None


@router.post("/admin/llm_cost")
async def admin_llm_cost(body: LlmCostBody) -> dict:
    """Admin endpoint used by demo drivers to simulate Nemotron consumption
    without having to run a full Hermes chat. In production this row is
    automatically derived by the read-only ATTACH to hermes-telemetry —
    this endpoint is only for the deterministic demo path."""
    row_id = db.insert_ledger_row(
        job_id=body.job_id,
        kind="llm_cost",
        amount_usd=float(body.amount_usd),
        source="sim_llm",
        ref=body.ref,
        session_id=body.session_id,
    )
    db.log_audit(
        "system", "llm_cost_recorded",
        {"job_id": body.job_id, "amount_usd": body.amount_usd, "ref": body.ref},
    )
    return {"recorded": "llm_cost", "id": row_id}


@router.get("/compute/fleet")
async def compute_fleet() -> dict:
    """Per-job rollup for the fleet view: tier + actual burn + margin."""
    allocs = db.get_compute_allocations()
    out: dict[str, dict] = {}
    for a in allocs:
        jid = a["job_id"]
        if jid in out and out[jid]["status"] == "active":
            continue  # newest active alloc wins
        burn = db.get_job_burn(jid)
        revenue = db.get_job_revenue(jid)
        budget = a.get("compute_budget_usd") or 0.0
        burn_ratio = (burn / budget) if budget > 0 else 0.0
        margin = revenue - burn - sum(
            row["amount_usd"]
            for row in db._get_conn().execute(
                "SELECT amount_usd FROM ledger WHERE job_id=? AND kind='external_spend'",
                (jid,),
            ).fetchall()
        )
        out[jid] = {
            "job_id": jid,
            "cost_center_id": a["cost_center_id"],
            "tier": a["tier"],
            "model": a["model"],
            "status": a["status"],
            "compute_budget_usd": budget,
            "actual_burn_usd": burn,
            "burn_ratio": burn_ratio,
            "actual_revenue_usd": revenue,
            "current_margin_usd": margin,
            "expected_revenue_usd": a.get("expected_revenue_usd"),
            "expected_margin_usd": a.get("expected_margin_usd"),
            "downgrade_reason": a.get("downgrade_reason"),
        }
    return {"items": list(out.values())}


# ---------------------------------------------------------------------------
# Active tokens — for the dashboard's token-vault widget
# ---------------------------------------------------------------------------


@router.get("/tokens/active")
async def tokens_active() -> dict:
    """List currently-active (not consumed, not expired) auth tokens.
    The dashboard renders them as a countdown widget."""
    import sqlite3
    import time as _time

    conn = db._get_conn()
    now_iso = db._now()
    rows = conn.execute(
        "SELECT token, issued_at, expires_at, job_id, cost_center_id,"
        " amount_usd, approval_id"
        " FROM auth_tokens"
        " WHERE consumed_at IS NULL AND expires_at > ?"
        " ORDER BY expires_at ASC",
        (now_iso,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Truncate the token in the response — never expose full tokens.
        d["token_preview"] = d["token"][:8] + "…"
        del d["token"]
        out.append(d)
    return {"items": out}


# ---------------------------------------------------------------------------
# Run Mermelada commission directly from the dashboard (no terminal needed)
# ---------------------------------------------------------------------------


@router.post("/demo/mermelada/run")
async def run_mermelada_demo() -> dict:
    """Trigger the Mermelada Studio commission flow end-to-end from the
    dashboard. Approvals still come through the existing queue; this
    endpoint just sequences the spends so the operator only needs the
    browser. Returns when the commission is complete (may block for
    several minutes if approvals are slow)."""
    import hook as _hook
    import anyio

    JOB = "mermelada-commission-001"

    def _spend(cost_center_id: str, amount: float, ref: str):
        return _hook.process_declaration_for_api(
            job_id=JOB,
            cost_center_id=cost_center_id,
            projected_usd=amount,
            ref=ref,
            task_id="dashboard-demo",
        )

    def _flow():
        outcomes = []
        # Stage 1 — sim revenue (if no real Stripe Checkout has been used)
        db.insert_ledger_row(
            job_id=JOB, kind="revenue", amount_usd=15.0,
            source="stripe", ref="pi_sim_mermelada_commission",
        )
        db.log_audit(
            "stripe", "revenue_received",
            {"job_id": JOB, "amount_usd": 15.0, "ref": "pi_sim_mermelada_commission"},
        )
        outcomes.append({"stage": "customer_paid", "amount": 15.0})

        # Stage 2 — image generation
        for i in range(3):
            r = _spend("image_gen", 0.30, f"bg_image_{i+1}")
            outcomes.append({"stage": "image_gen", "outcome": r, "amount": 0.30})

        r = _spend("image_gen", 2.00, "hero_illustration")
        outcomes.append({"stage": "hero", "outcome": r, "amount": 2.00})

        # Stage 3 — saas provisioning (reject then retry)
        r = _spend("saas_dev_tools", 40.0, "premium_fonts_pack")
        outcomes.append({"stage": "fonts_premium", "outcome": r, "amount": 40.0})

        r = _spend("saas_dev_tools", 5.0, "standard_fonts")
        outcomes.append({"stage": "fonts_standard", "outcome": r, "amount": 5.0})

        # Stage 4 — compute
        r = _spend("compute", 0.10, "render_pipeline")
        outcomes.append({"stage": "compute", "outcome": r, "amount": 0.10})

        # Stage 5 — greedy ads (will be blocked)
        r = _spend("marketing", 25.0, "ads_boost_attempt")
        outcomes.append({"stage": "ads_boost", "outcome": r, "amount": 25.0})

        return outcomes

    outcomes = await anyio.to_thread.run_sync(_flow)
    return {"status": "completed", "outcomes": outcomes}


@router.post("/demo/reset")
async def demo_reset() -> dict:
    """Wipe the demo state so the dashboard is clean for the next take.

    Deletes everything from ledger, approval_requests, audit_trail, and
    auth_tokens. Re-inserts the Nemotron session anchor row if telemetry
    has at least one priced session, so the LLM cost column lights up
    immediately on the next /pnl call."""
    conn = db._get_conn()
    conn.execute("DELETE FROM ledger")
    conn.execute("DELETE FROM approval_requests")
    conn.execute("DELETE FROM audit_trail")
    conn.execute("DELETE FROM auth_tokens")

    # Try to re-anchor a recent priced telemetry session so the LLM
    # column isn't $0.00 on the next run.
    re_anchored = None
    tele_path = _cfg.telemetry_db_path()
    if tele_path.exists():
        try:
            conn.execute(
                "ATTACH DATABASE ? AS telemetry",
                (f"file:{tele_path}?mode=ro",),
            )
            row = conn.execute(
                "SELECT session_id FROM telemetry.runs"
                " WHERE cost_usd > 0 ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row and row["session_id"]:
                db.insert_ledger_row(
                    job_id="mermelada-commission-001",
                    kind="external_spend",
                    amount_usd=0.0,
                    source="session_anchor",
                    ref=None,
                    session_id=row["session_id"],
                )
                re_anchored = row["session_id"]
        except Exception:
            pass
        finally:
            try:
                conn.execute("DETACH DATABASE telemetry")
            except Exception:
                pass

    db.log_audit("system", "demo_reset", {"re_anchored_session": re_anchored})
    return {"status": "reset", "anchored_session": re_anchored}


# ---------------------------------------------------------------------------
# Layer 2 — Stripe Issuing authorization webhook (network-layer backstop)
# ---------------------------------------------------------------------------


class IssuingAuthorizationEvent(BaseModel):
    """Subset of the Stripe Issuing authorization webhook payload we read.

    Stripe sends ``issuing_authorization.request`` for every charge attempt
    against a managed virtual card. We have a few seconds to respond with
    ``approve`` or ``decline``. With Argus wired in, a rogue agent that
    bypasses Hermes entirely and hits Stripe API directly still can't move
    money — the card itself declines at the network layer."""

    type: str
    data: dict


@router.post("/webhooks/stripe-issuing-authorization")
async def stripe_issuing_authorization(evt: IssuingAuthorizationEvent) -> dict:
    """Defense-in-depth: validate the Argus auth token attached to the
    Stripe Issuing authorization. Approve only if a valid token covers
    this exact amount + job."""
    payload = evt.data or {}
    obj = payload.get("object") if isinstance(payload.get("object"), dict) else payload
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}

    token = metadata.get("argus_auth_token")
    job_id = metadata.get("job_id")
    amount_cents = obj.get("amount") or obj.get("pending_request", {}).get("amount") or 0
    amount_usd = float(amount_cents) / 100.0 if isinstance(amount_cents, int) else float(amount_cents or 0)
    auth_id = obj.get("id")

    audit = {
        "auth_id": auth_id,
        "job_id": job_id,
        "amount_usd": amount_usd,
        "has_token": bool(token),
    }

    if evt.type != "issuing_authorization.request":
        db.log_audit("stripe-issuing", "webhook_ignored", {"type": evt.type, **audit})
        return {"recorded": "ignored", "type": evt.type}

    check = db.validate_and_consume_auth_token(
        token or "",
        actual_amount_usd=amount_usd,
        actual_job_id=job_id,
        ref=auth_id,
    )

    if check.valid:
        db.log_audit(
            "stripe-issuing", "card_authorized",
            {**audit, "token": (token or "")[:8] + "..."},
        )
        # Stripe expects { "approved": true } on this webhook (test mode
        # respects this; live mode also requires real-time-auth setup).
        return {"approved": True}

    db.log_audit(
        "stripe-issuing", "card_declined",
        {**audit, "reason": check.reason},
    )
    return {"approved": False, "decline_reason": check.reason}
