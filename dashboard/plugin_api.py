"""Argus dashboard plugin — backend API routes.

Mounted at /api/plugins/argus/ by Hermes. Reads from the ledger and writes
approval decisions / Stripe webhooks. No long-running work — the
synchronous-hold lives in the agent's pre_tool_call hook (see hook.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
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
    """Three-sided P&L per job: revenue − llm_cost − external_spend. llm_cost is
    derived from hermes-telemetry via the read-only A1 ATTACH (pnl_by_job)."""
    rows = db.pnl_by_job()
    total = {
        "revenue": round(sum(r["revenue"] for r in rows), 2),
        "llm_cost": round(sum(r["llm_cost"] for r in rows), 2),
        "external_spend": round(sum(r["external_spend"] for r in rows), 2),
        "pnl": round(sum(r["pnl"] for r in rows), 2),
    }
    return {"jobs": rows, "total": total}


# ---------------------------------------------------------------------------
# Treasury — the SOLVENT-style company close (CLAUDE.md §9.2), all computed
# ---------------------------------------------------------------------------


@router.get("/treasury")
async def treasury() -> dict:
    """Company cash close: cash_position = seed_capital + gross_revenue −
    total_spend. llm_cost uses the SAME single basis as /pnl (db.money_totals →
    _total_llm_cost: A1 telemetry when present, else A2 ledger rows), so /pnl and
    /treasury always agree on inference cost — no over-stated profit under A1."""
    totals = db.money_totals()
    seed = round(_cfg.seed_capital(), 2)
    total_spend = round(totals["llm_cost"] + totals["external_spend"], 2)
    net_pnl = round(totals["revenue"] - total_spend, 2)
    return {
        "seed_capital": seed,
        "gross_revenue": totals["revenue"],
        "total_spend": total_spend,
        "net_pnl": net_pnl,
        "cash_position": db.cash_position(),
    }


# ---------------------------------------------------------------------------
# Fleet — ALL jobs (the dashboard fleet timeline reads this, not /compute/fleet).
# Superset of /compute/fleet: a cash-only job with no compute allocation MUST
# appear. All reads; no writes.
# ---------------------------------------------------------------------------


def _jobs_snapshot() -> list:
    """Every job with ledger activity OR a compute allocation (the union — so
    this is a strict superset of /compute/fleet). Each row carries per-job P&L,
    cost center, status, and its latest compute allocation (or None)."""
    pnl_rows = {r["job_id"]: r for r in db.pnl_by_job()}

    # Latest allocation per job (get_compute_allocations is ordered id DESC, so
    # the first seen for a job_id is the newest). One query, no N+1.
    latest_alloc: dict = {}
    for a in db.get_compute_allocations():
        latest_alloc.setdefault(a["job_id"], a)

    job_ids = list(pnl_rows.keys())
    for jid in latest_alloc:
        if jid not in pnl_rows:
            job_ids.append(jid)          # allocation-only job (no ledger row yet)

    out = []
    for jid in job_ids:
        r = pnl_rows.get(jid)
        alloc = latest_alloc.get(jid)
        cc = (alloc["cost_center_id"] if alloc else None) or db.get_cost_center_for_job(jid)
        out.append({
            "job_id": jid,
            "cost_center_id": cc,
            "revenue": r["revenue"] if r else 0.0,
            "llm_cost": r["llm_cost"] if r else 0.0,
            "external_spend": r["external_spend"] if r else 0.0,
            "margin": r["pnl"] if r else 0.0,
            # status from the compute allocation when present; cash-only jobs
            # have no tier, so they read as plain "active".
            "status": alloc["status"] if alloc else "active",
            "allocation": alloc,
        })
    out.sort(key=lambda d: d["job_id"])
    return out


@router.get("/jobs")
async def jobs() -> dict:
    """All jobs (ledger + allocation union) for the fleet timeline."""
    return {"items": _jobs_snapshot()}


# ---------------------------------------------------------------------------
# Aggregate state — ONE response the SPA polls per tick (instead of ~6 calls).
# ---------------------------------------------------------------------------


@router.get("/state")
async def state(audit_limit: int = 50) -> dict:
    """Full dashboard snapshot in a single read. ``eye_state`` is ``holding``
    when any approval is pending (the Argus eye pauses), else ``watching``."""
    pending = db.get_pending_approvals()
    return {
        "pnl": await pnl(),
        "treasury": await treasury(),
        "approvals": {"pending": pending},
        "audit": {"items": db.get_recent_audit(limit=max(1, min(500, audit_limit)))},
        "fleet": {"items": _jobs_snapshot()},
        "tokens": await tokens_active(),
        "eye_state": "holding" if pending else "watching",
    }


# ---------------------------------------------------------------------------
# Revenue intake (CLAUDE.md §9.2) — the third P&L input. Two paths:
#   A) /revenue/sim    — demo-only, no Stripe round-trip
#   B) /revenue/stripe — REAL webhook, signature-verified (mandatory)
# Both idempotent on ref so a replay never double-counts.
# ---------------------------------------------------------------------------


class RevenueSimBody(BaseModel):
    # job_id Optional so a missing value returns a clean 400 (not a 422).
    job_id: Optional[str] = None
    amount_usd: float
    ref: Optional[str] = None
    source: Optional[str] = None


@router.post("/revenue/sim")
async def revenue_sim(body: RevenueSimBody) -> dict:
    """DEMO-ONLY revenue intake. Writes a 'revenue' ledger row attributed to the
    job, idempotent on ref. This is what the scripted demo driver calls."""
    if not body.job_id or not body.job_id.strip():
        raise HTTPException(status_code=400, detail="job_id required")
    if body.ref and db.revenue_recorded(body.ref):
        return {"recorded": "duplicate", "ref": body.ref}
    source = body.source or "stripe-sim"
    row_id = db.append_fact(
        body.job_id, "revenue", float(body.amount_usd), source=source, ref=body.ref
    )
    db.append_audit(
        "stripe-sim", "revenue_received",
        {"job_id": body.job_id, "amount_usd": round(float(body.amount_usd), 2),
         "ref": body.ref},
    )
    return {"recorded": "revenue", "id": row_id, "job_id": body.job_id, "ref": body.ref}


# Sentinel attribution for revenue that arrives with no job_id — keeps treasury
# correct without polluting any real job's per-job P&L (§9.2 attribution rule).
_UNATTRIBUTED_JOB = "unattributed"
_UNATTRIBUTED_CC = "unattributed"


def _verify_stripe_signature(
    raw_body: bytes, sig_header: str, secret: str, tolerance: int = 300
) -> bool:
    """Verify a Stripe ``Stripe-Signature`` header (scheme: ``t=<ts>,v1=<hmac>``)
    against the test-mode signing secret. signed_payload = ``"{t}.{body}"``,
    HMAC-SHA256 with the secret. Constant-time compare; replay window = tolerance
    seconds. No `stripe` lib dependency — pure stdlib."""
    if not secret or not sig_header:
        return False
    pairs = [p.split("=", 1) for p in sig_header.split(",") if "=" in p]
    ts = next((v for k, v in pairs if k == "t"), None)
    v1s = [v for k, v in pairs if k == "v1"]
    if not ts or not v1s:
        return False
    try:
        if tolerance and abs(time.time() - int(ts)) > tolerance:
            return False
    except ValueError:
        return False
    signed = f"{ts}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v1) for v1 in v1s)


def _stripe_revenue_fields(evt_type: str, obj: dict) -> tuple[float, Optional[str], Optional[str]]:
    """(amount_usd, ref, job_id) from a verified Stripe object. Amounts are
    cents (int) → dollars; ref is the object id (stable across redelivery)."""
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    if evt_type == "checkout.session.completed":
        cents = obj.get("amount_total") or obj.get("amount") or 0
    else:  # payment_intent.succeeded
        cents = obj.get("amount_received") or obj.get("amount") or 0
    amount = cents / 100.0 if isinstance(cents, int) else float(cents or 0)
    ref = obj.get("id")
    job_id = metadata.get("job_id")
    return round(float(amount), 2), (str(ref) if ref else None), (str(job_id) if job_id else None)


@router.post("/revenue/stripe")
async def revenue_stripe(request: Request) -> dict:
    """REAL Stripe webhook. Verifies the signature (MANDATORY — invalid → 400,
    no row), handles checkout.session.completed / payment_intent.succeeded,
    idempotent on the Stripe object id. Revenue with metadata.job_id is
    attributed to that job; without it, recorded to the 'unattributed' sentinel
    (treasury stays correct, per-job P&L isn't polluted)."""
    raw = await request.body()
    secret = _cfg.stripe_webhook_secret()
    sig = request.headers.get("stripe-signature", "")
    if not _verify_stripe_signature(raw, sig, secret or ""):
        # Fail closed: no signature secret / bad signature → reject, write nothing.
        raise HTTPException(status_code=400, detail="invalid stripe signature")

    try:
        evt = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    evt_type = evt.get("type")
    data = evt.get("data") or {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else data

    if evt_type not in ("checkout.session.completed", "payment_intent.succeeded"):
        db.append_audit("stripe", "webhook_ignored", {"type": evt_type})
        return {"recorded": "ignored", "type": evt_type}

    amount, ref, job_id = _stripe_revenue_fields(evt_type, obj or {})

    if ref and db.revenue_recorded(ref):
        return {"recorded": "duplicate", "ref": ref}

    if job_id:
        db.append_fact(job_id, "revenue", amount, source="stripe", ref=ref)
        db.append_audit(
            "stripe", "revenue_received",
            {"job_id": job_id, "amount_usd": amount, "ref": ref},
        )
        return {"recorded": "revenue", "job_id": job_id, "ref": ref}

    # No job_id → do NOT guess. Record to the sentinel so cash stays right.
    db.register_job(_UNATTRIBUTED_JOB, _UNATTRIBUTED_CC)
    db.append_fact(_UNATTRIBUTED_JOB, "revenue", amount, source="stripe", ref=ref)
    db.append_audit("stripe", "revenue_unattributed", {"ref": ref, "amount_usd": amount})
    return {"recorded": "unattributed", "ref": ref}


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
    this endpoint is only for the deterministic demo path. Each insert
    also runs the throttle check so mid-flight downgrades fire when the
    burn ratio crosses the threshold."""
    import hook as _hook

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
    throttle = _hook.check_and_apply_throttle(body.job_id)
    return {"recorded": "llm_cost", "id": row_id, "throttle": throttle}


@router.get("/jobs/{job_id}/status")
async def get_job_status(job_id: str) -> dict:
    """The cooperative throttle endpoint. An agent polls this each turn
    while running an LLM-heavy job. Argus returns the recommended action:

      - ``active``: continue on the originally-authorized model.
      - ``downgraded``: switch to the base model for the rest of the
        job (Argus already logged the downgrade event).
      - ``killed``: stop the job entirely; current_margin is in the red.

    Note: real runtime enforcement (Argus forcing the model swap mid-call)
    requires Hermes to expose a model_override hook — see FUTURE.md
    Tier 1. Today the agent has to cooperate."""
    import hook as _hook
    alloc = db.get_latest_active_allocation(job_id)
    throttle = _hook.check_and_apply_throttle(job_id)
    return {
        "job_id": job_id,
        "allocation": alloc,
        **throttle,
    }


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
            "actual_model": a.get("actual_model"),
            "integrity_status": a.get("integrity_status"),
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


@router.post("/admin/compute_integrity_sweep")
async def compute_integrity_sweep() -> dict:
    """Run a compute-integrity inspection: for each active allocation,
    compare the model the agent was authorized to use against the model
    that telemetry says actually ran. Mismatches are logged to the audit
    trail as `compute_integrity_violation` events. See CLAUDE.md §3.2."""
    violations = db.run_compute_integrity_sweep()
    return {"status": "ok", "violations": violations}


@router.post("/admin/set_actual_model")
async def admin_set_actual_model(body: dict) -> dict:
    """Demo-only: record the model that 'actually ran' for an allocation,
    bypassing the telemetry ATTACH. The deterministic demo uses this to
    inject a silent-fallback scenario (authorized Ultra, observed Base)
    so the integrity sweep has something to flag without needing a live
    Hermes chat."""
    alloc_id = int(body.get("allocation_id") or 0)
    actual_model = str(body.get("actual_model") or "")
    if not alloc_id or not actual_model:
        return {"error": "allocation_id + actual_model required"}
    db.set_actual_model(alloc_id, actual_model)
    return {"ok": True, "allocation_id": alloc_id, "actual_model": actual_model}


@router.post("/demo/ai-services-firm/run")
async def run_ai_services_firm_demo() -> dict:
    """Drive the AI services firm demo end-to-end from the dashboard.
    Three jobs with different margin profiles flow through the compute
    allocator: premium research → Ultra, thin-margin generation → Base
    (after self-correct), negative-margin vanity research → Reject.
    No terminal needed."""
    import hook as _hook
    import anyio

    def _flow():
        outcomes = []

        # ---- Job A: premium research ($200 revenue, $15 burn) ----
        a = _hook.process_compute_request_for_api(
            job_id="research-enterprise-001",
            cost_center_id="ai_research",
            expected_revenue_usd=200.0,
            projected_burn_usd=15.0,
            ref="competitor_landscape_q3",
            task_id="dashboard-demo-a",
        )
        outcomes.append({"job": "research-enterprise-001", "compute": a})
        # Demo wrinkle: Job A was authorized for Ultra, but we simulate
        # a silent fallback to Base — the agent thought it was on Ultra,
        # actually ran on Base. The integrity sweep flags it.
        alloc_a_id = a.get("allocation_id")
        if alloc_a_id:
            db.set_actual_model(alloc_a_id, "nvidia/nemotron-3-base-9b")
        db.insert_ledger_row(
            job_id="research-enterprise-001", kind="revenue",
            amount_usd=200.0, source="stripe",
            ref="pi_sim_research_001",
        )
        db.log_audit("stripe", "revenue_received",
                     {"job_id": "research-enterprise-001", "amount_usd": 200.0})
        # Simulate actual Nemotron Ultra burn ~$14.80
        db.insert_ledger_row(
            job_id="research-enterprise-001", kind="llm_cost",
            amount_usd=14.80, source="sim_llm",
            ref="nemotron_ultra_session_a",
        )
        db.log_audit("system", "llm_cost_recorded",
                     {"job_id": "research-enterprise-001", "amount_usd": 14.80,
                      "ref": "nemotron_ultra_session_a"})

        # ---- Job B: low margin → self-correct to base ----
        b1 = _hook.process_compute_request_for_api(
            job_id="gen-tweet-042", cost_center_id="ai_generation",
            expected_revenue_usd=3.0, projected_burn_usd=5.0,
            ref="viral_tweet", task_id="dashboard-demo-b",
        )
        outcomes.append({"job": "gen-tweet-042 (over-spec)", "compute": b1})
        # Agent self-corrects with smaller burn
        b2 = _hook.process_compute_request_for_api(
            job_id="gen-tweet-042", cost_center_id="ai_generation",
            expected_revenue_usd=3.0, projected_burn_usd=0.30,
            ref="viral_tweet_v2", task_id="dashboard-demo-b",
        )
        outcomes.append({"job": "gen-tweet-042 (self-corrected)", "compute": b2})
        db.insert_ledger_row(
            job_id="gen-tweet-042", kind="revenue",
            amount_usd=3.0, source="stripe", ref="pi_sim_gen_042",
        )
        db.log_audit("stripe", "revenue_received",
                     {"job_id": "gen-tweet-042", "amount_usd": 3.0})
        db.insert_ledger_row(
            job_id="gen-tweet-042", kind="llm_cost",
            amount_usd=0.28, source="sim_llm",
            ref="nemotron_base_session_b",
        )
        db.log_audit("system", "llm_cost_recorded",
                     {"job_id": "gen-tweet-042", "amount_usd": 0.28,
                      "ref": "nemotron_base_session_b"})

        # ---- Job C: vanity research → reject ----
        c = _hook.process_compute_request_for_api(
            job_id="research-vanity-099", cost_center_id="ai_research",
            expected_revenue_usd=2.0, projected_burn_usd=20.0,
            ref="vanity_lookup", task_id="dashboard-demo-c",
        )
        outcomes.append({"job": "research-vanity-099", "compute": c})

        # Record Job B's actual model = the authorized base model (no
        # mismatch — keeps the demo honest, only Job A flags violation).
        b_alloc = db.get_latest_active_allocation("gen-tweet-042")
        if b_alloc:
            db.set_actual_model(b_alloc["id"], "nvidia/nemotron-3-base-9b")

        # ---- Job D: mid-flight throttle scenario ----
        # Authorized Ultra with a tight $5 budget, then the agent's
        # research runs hotter than expected. Halfway through Argus
        # sees the burn ratio breach and downgrades.
        d = _hook.process_compute_request_for_api(
            job_id="research-runaway-007",
            cost_center_id="ai_research",
            expected_revenue_usd=120.0,
            projected_burn_usd=5.0,
            ref="exploratory_research",
            task_id="dashboard-demo-d",
        )
        outcomes.append({"job": "research-runaway-007 (allocated)", "compute": d})
        d_alloc = db.get_latest_active_allocation("research-runaway-007")
        if d_alloc:
            db.set_actual_model(d_alloc["id"], "nvidia/nemotron-3-ultra-550b-a55b")
        # First chunk of inference — within budget, no throttle.
        db.insert_ledger_row(
            job_id="research-runaway-007", kind="llm_cost",
            amount_usd=2.50, source="sim_llm",
            ref="nemotron_ultra_chunk_1",
        )
        db.log_audit("system", "llm_cost_recorded",
                     {"job_id": "research-runaway-007", "amount_usd": 2.50,
                      "ref": "nemotron_ultra_chunk_1"})
        t1 = _hook.check_and_apply_throttle("research-runaway-007")
        outcomes.append({"job": "research-runaway-007 (chunk 1)", "throttle": t1})
        # Second chunk pushes the ratio past 0.7 → DOWNGRADE.
        db.insert_ledger_row(
            job_id="research-runaway-007", kind="llm_cost",
            amount_usd=1.80, source="sim_llm",
            ref="nemotron_ultra_chunk_2",
        )
        db.log_audit("system", "llm_cost_recorded",
                     {"job_id": "research-runaway-007", "amount_usd": 1.80,
                      "ref": "nemotron_ultra_chunk_2"})
        t2 = _hook.check_and_apply_throttle("research-runaway-007")
        outcomes.append({"job": "research-runaway-007 (chunk 2 — throttle)", "throttle": t2})
        # Revenue lands so margin is still positive.
        db.insert_ledger_row(
            job_id="research-runaway-007", kind="revenue",
            amount_usd=120.0, source="stripe",
            ref="pi_sim_research_runaway",
        )
        db.log_audit("stripe", "revenue_received",
                     {"job_id": "research-runaway-007", "amount_usd": 120.0})

        # Now run the integrity sweep. Job A will flag as silent fallback;
        # Job B is clean; Job C never had an active allocation (rejected);
        # Job D's actual model matches authorized (the downgrade was an
        # economic decision, not a silent substitution).
        violations = db.run_compute_integrity_sweep()
        outcomes.append({"integrity_violations": violations})

        return outcomes

    outcomes = await anyio.to_thread.run_sync(_flow)
    return {"status": "completed", "outcomes": outcomes}


@router.post("/demo/reset")
async def demo_reset() -> dict:
    """Wipe the demo state so the dashboard is clean for the next take.

    Deletes everything from ledger, approval_requests, audit_trail, auth_tokens,
    and compute_allocations. Re-inserts the Nemotron session anchor row if telemetry
    has at least one priced session, so the LLM cost column lights up
    immediately on the next /pnl call."""
    conn = db._get_conn()
    conn.execute("DELETE FROM ledger")
    conn.execute("DELETE FROM approval_requests")
    conn.execute("DELETE FROM audit_trail")
    conn.execute("DELETE FROM auth_tokens")
    conn.execute("DELETE FROM compute_allocations")

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
