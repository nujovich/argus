"""Argus Capture + Enforcement — the pre_tool_call hook.

Fires before every tool. Two enforcement modes:

1. ``argus_request_spend`` (the cooperative path) — the agent declares its
   intent BEFORE the Stripe call. Policy gates the declaration; on ALLOW
   Argus issues a short-lived single-use auth token the agent must include
   in the next Stripe call's ``metadata.argus_auth_token``.

2. ``stripe_*`` (the backstop) — every Stripe-skill invocation MUST carry
   a valid Argus auth token in its arguments. Without it, the spend is
   blocked. A rogue agent that skips step 1 has no way to pass this check.

See CLAUDE.md §6 for the design rationale.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import config as _cfg
import db
import policy


# The cooperative declaration tool the agent should call.
_DECL_TOOL = "argus_request_spend"
# The Stripe-skill prefix the hook backstops on.
_STRIPE_PREFIX = "stripe_"


# How long the hook will block waiting for a human decision before
# auto-rejecting. Configurable via env later; demo default 5 min.
APPROVAL_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 0.5

# Auth-token lifetime: short by design. The agent must declare and spend
# in quick succession.
AUTH_TOKEN_TTL_SEC = 60
AUTH_TOKEN_TOLERANCE = 0.10  # ±10% on amount


def _is_decl_call(tool_name: str) -> bool:
    return tool_name == _DECL_TOOL


def _is_stripe_call(tool_name: str) -> bool:
    return tool_name.startswith(_STRIPE_PREFIX)


def _is_spend_call(tool_name: str) -> bool:
    return _is_decl_call(tool_name) or _is_stripe_call(tool_name)


def _extract_declaration(
    tool_name: str, args: Dict[str, Any]
) -> Optional[policy.SpendDeclaration]:
    """Pull (job_id, cost_center_id, projected_usd, ref) out of tool args.

    Returns None when the call isn't a declared spend.
    """
    if not isinstance(args, dict):
        return None
    job_id = args.get("job_id")
    cost_center_id = args.get("cost_center_id") or args.get("cost_center") or "default"
    projected = args.get("projected_usd") or args.get("amount_usd") or args.get("amount")
    if job_id is None or projected is None:
        return None
    try:
        projected_f = float(projected)
    except (TypeError, ValueError):
        return None
    return policy.SpendDeclaration(
        job_id=str(job_id),
        cost_center_id=str(cost_center_id),
        projected_usd=projected_f,
        tool_name=tool_name,
        ref=str(args.get("ref")) if args.get("ref") is not None else None,
    )


def _extract_stripe_args(args: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """For stripe_* tool calls, dig out the auth token + amount + job_id from
    typical Stripe API argument shapes."""
    if not isinstance(args, dict):
        return None, None, None
    metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    token = metadata.get("argus_auth_token") or args.get("argus_auth_token")
    job_id = metadata.get("job_id") or args.get("job_id")

    # Stripe's convention: ``amount`` is always in the smallest currency
    # unit (cents). Our own ``amount_usd`` / ``projected_usd`` are dollars
    # as float. Disambiguate by arg name, not magnitude.
    amount_usd: Optional[float] = None
    if "amount_usd" in args:
        try:
            amount_usd = float(args["amount_usd"])
        except (TypeError, ValueError):
            amount_usd = None
    elif "projected_usd" in args:
        try:
            amount_usd = float(args["projected_usd"])
        except (TypeError, ValueError):
            amount_usd = None
    elif "amount" in args:
        try:
            amount_usd = float(args["amount"]) / 100.0  # Stripe cents
        except (TypeError, ValueError):
            amount_usd = None

    return token, amount_usd, (str(job_id) if job_id else None)


def _build_snapshot(decl: policy.SpendDeclaration) -> Tuple[policy.BudgetSnapshot, list]:
    budgets = _cfg.load_budgets()
    budget = budgets.get(decl.cost_center_id) or budgets.get("default")
    if budget is None:
        return (
            policy.BudgetSnapshot(
                cost_center_id=decl.cost_center_id,
                limit_usd=float("inf"),
                spent_usd=0.0,
                auto_approve_under_usd=float("inf"),
                manager_under_usd=None,
            ),
            [],
        )
    spent = db.get_cost_center_spent(budget.cost_center_id, [decl.job_id])
    snap = policy.BudgetSnapshot(
        cost_center_id=budget.cost_center_id,
        limit_usd=budget.limit_usd,
        spent_usd=spent,
        auto_approve_under_usd=budget.auto_approve_under_usd,
        manager_under_usd=budget.manager_under_usd,
    )
    return snap, []


def _wait_for_decision(req_id: str, *, timeout: float, poll: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = db.get_approval_status(req_id)
        if status and status != "pending":
            return status
        time.sleep(poll)
    db.mark_timeout(req_id)
    return "timeout"


def _process_declaration(
    decl: policy.SpendDeclaration, task_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Run a declaration through Policy + Enforcement. Returns
    (block_response, allow_metadata). On ALLOW the auth token is in the
    second tuple element; the caller (HTTP route) can hand it back to the
    agent."""
    snap, _ = _build_snapshot(decl)
    decision = policy.decide(decl, snap)

    audit_payload = {
        "tool": decl.tool_name,
        "task_id": task_id,
        "job_id": decl.job_id,
        "cost_center_id": decl.cost_center_id,
        "projected_usd": decl.projected_usd,
        "verdict": decision.verdict,
        "reason": decision.reason,
    }
    db.log_audit("system", "spend_evaluated", audit_payload)

    if not decision.needs_approval:
        token = db.issue_auth_token(
            job_id=decl.job_id,
            cost_center_id=decl.cost_center_id,
            amount_usd=decl.projected_usd,
            ttl_seconds=AUTH_TOKEN_TTL_SEC,
            tolerance_pct=AUTH_TOKEN_TOLERANCE,
        )
        db.log_audit(
            "system", "auth_token_issued",
            {"job_id": decl.job_id, "amount_usd": decl.projected_usd,
             "ttl_sec": AUTH_TOKEN_TTL_SEC, "verdict": "ALLOW"},
        )
        # Declaration auto-approved → record the intent (sim path).
        db.insert_ledger_row(
            job_id=decl.job_id,
            kind="external_spend",
            amount_usd=decl.projected_usd,
            source="argus_declaration",
            ref=decl.ref,
            session_id=task_id or None,
        )
        return None, token

    req_id = db.create_approval_request(
        job_id=decl.job_id,
        cost_center_id=decl.cost_center_id,
        projected_usd=decl.projected_usd,
        level=decision.level or "finance",
        tool_name=decl.tool_name,
        ref=decl.ref,
    )
    db.log_audit(
        "system", "approval_requested",
        {**audit_payload, "approval_id": req_id, "level": decision.level},
    )

    status = _wait_for_decision(
        req_id, timeout=APPROVAL_TIMEOUT_SEC, poll=POLL_INTERVAL_SEC
    )

    if status == "approved":
        token = db.issue_auth_token(
            job_id=decl.job_id,
            cost_center_id=decl.cost_center_id,
            amount_usd=decl.projected_usd,
            ttl_seconds=AUTH_TOKEN_TTL_SEC,
            tolerance_pct=AUTH_TOKEN_TOLERANCE,
            approval_id=req_id,
        )
        db.log_audit(
            "system", "auth_token_issued",
            {"approval_id": req_id, "job_id": decl.job_id,
             "amount_usd": decl.projected_usd, "ttl_sec": AUTH_TOKEN_TTL_SEC,
             "verdict": "HUMAN_APPROVED"},
        )
        db.insert_ledger_row(
            job_id=decl.job_id,
            kind="external_spend",
            amount_usd=decl.projected_usd,
            source="argus_declaration",
            ref=decl.ref,
            session_id=task_id or None,
        )
        db.log_audit("system", "spend_resumed", {"approval_id": req_id})
        return None, token

    msg = f"Argus blocked spend ({status}): {decision.reason}"
    db.log_audit("system", f"spend_{status}", {"approval_id": req_id})
    return {"action": "block", "message": msg}, None


def _process_stripe_backstop(
    tool_name: str, args: Dict[str, Any], task_id: str
) -> Optional[Dict[str, Any]]:
    """Backstop enforcement on raw stripe_* tool calls. Requires a valid
    auth token in args.metadata.argus_auth_token. Without it → BLOCK."""
    token, amount_usd, job_id = _extract_stripe_args(args or {})
    audit_base = {
        "tool": tool_name,
        "task_id": task_id,
        "job_id": job_id,
        "amount_usd": amount_usd,
        "has_token": bool(token),
    }

    if not token:
        db.log_audit("system", "stripe_blocked_no_token", audit_base)
        return {
            "action": "block",
            "message": (
                "Argus blocked Stripe call: no argus_auth_token in metadata."
                " Call argus_request_spend first to obtain one."
            ),
        }

    if amount_usd is None:
        db.log_audit("system", "stripe_blocked_no_amount", audit_base)
        return {
            "action": "block",
            "message": "Argus blocked Stripe call: amount not detected in args.",
        }

    check = db.validate_and_consume_auth_token(
        token,
        actual_amount_usd=amount_usd,
        actual_job_id=job_id,
        ref=args.get("idempotency_key") if isinstance(args, dict) else None,
    )

    if check.valid:
        db.log_audit(
            "system", "stripe_authorized",
            {**audit_base, "token": token[:8] + "...", "amount_usd": amount_usd},
        )
        return None  # ALLOW — Hermes proceeds with the Stripe call

    db.log_audit(
        "system", "stripe_blocked_bad_token",
        {**audit_base, "reason": check.reason},
    )
    return {
        "action": "block",
        "message": f"Argus blocked Stripe call: {check.reason}.",
    }


def on_pre_tool_call(tool_name: str, args: Dict[str, Any], task_id: str, **_: Any):
    """Hermes pre_tool_call callback. Returns None to allow, dict to block."""
    if not _is_spend_call(tool_name):
        return None

    if _is_decl_call(tool_name):
        decl = _extract_declaration(tool_name, args or {})
        if decl is None:
            db.log_audit(
                "system", "spend_skipped_missing_declaration",
                {"tool": tool_name, "task_id": task_id},
            )
            return {
                "action": "block",
                "message": (
                    "Argus blocked argus_request_spend: missing required args"
                    " (job_id, projected_usd)."
                ),
            }
        block, _token = _process_declaration(decl, task_id)
        return block

    # _is_stripe_call(tool_name) — backstop layer
    return _process_stripe_backstop(tool_name, args or {}, task_id)


def process_declaration_for_api(
    *, job_id: str, cost_center_id: str, projected_usd: float, ref: Optional[str],
    task_id: str
) -> Dict[str, Any]:
    """Helper for plugin_api.py / sim path: runs the declaration and returns
    a JSON-shaped result {action, [auth_token, expires_in, ...]}."""
    decl = policy.SpendDeclaration(
        job_id=job_id,
        cost_center_id=cost_center_id,
        projected_usd=projected_usd,
        tool_name=_DECL_TOOL,
        ref=ref,
    )
    block, token = _process_declaration(decl, task_id)
    if block is not None:
        return block
    out: Dict[str, Any] = {"action": "allow"}
    if token:
        out.update(token)
    return out


# ---------------------------------------------------------------------------
# Compute Allocator entrypoint (Phase 4.5)
# ---------------------------------------------------------------------------


def _build_compute_snapshot(
    decl: policy.ComputeDeclaration,
) -> Optional[policy.ComputeSnapshot]:
    budgets = _cfg.load_budgets()
    budget = budgets.get(decl.cost_center_id) or budgets.get("default")
    if budget is None or budget.ultra_model is None or budget.base_model is None:
        return None
    return policy.ComputeSnapshot(
        cost_center_id=budget.cost_center_id,
        ultra_model=budget.ultra_model,
        base_model=budget.base_model,
        ultra_min_revenue_usd=budget.ultra_min_revenue_usd or 0.0,
        ultra_min_margin_usd=budget.ultra_min_margin_usd or 0.0,
        reject_below_margin_usd=budget.reject_below_margin_usd or 0.0,
        monthly_spent_usd=0.0,    # TODO Phase 5: aggregate from ledger
        monthly_limit_usd=budget.limit_usd,
    )


def process_compute_request_for_api(
    *,
    job_id: str,
    cost_center_id: str,
    expected_revenue_usd: float,
    projected_burn_usd: float,
    ref: Optional[str],
    task_id: str,
) -> Dict[str, Any]:
    """Run a compute-tier declaration through Policy + the allocator.

    Returns JSON with at minimum {action, tier, model, compute_budget_usd}.
    On ALLOW also returns an auth_token the agent must echo on subsequent
    Nemotron calls (so compute-integrity inspection can validate them
    against telemetry.runs).
    """
    decl = policy.ComputeDeclaration(
        job_id=job_id,
        cost_center_id=cost_center_id,
        expected_revenue_usd=expected_revenue_usd,
        projected_burn_usd=projected_burn_usd,
        tool_name="argus_request_compute",
        ref=ref,
    )
    snap = _build_compute_snapshot(decl)
    if snap is None:
        msg = (
            f"cost_center '{cost_center_id}' has no compute tier config "
            "(needs ultra_model/base_model and thresholds in cost_centers.yaml)"
        )
        db.log_audit(
            "system", "compute_request_misconfigured",
            {"job_id": job_id, "cost_center_id": cost_center_id, "reason": msg},
        )
        return {"action": "block", "message": f"Argus: {msg}"}

    decision = policy.decide_compute_tier(decl, snap)
    audit_payload = {
        "job_id": job_id,
        "cost_center_id": cost_center_id,
        "expected_revenue_usd": expected_revenue_usd,
        "projected_burn_usd": projected_burn_usd,
        "verdict": decision.verdict,
        "tier": decision.tier_label,
        "model": decision.model,
        "expected_margin_usd": decision.expected_margin_usd,
        "reason": decision.reason,
    }
    db.log_audit("system", "compute_tier_evaluated", audit_payload)

    if decision.is_rejected:
        db.insert_compute_allocation(
            job_id=job_id,
            cost_center_id=cost_center_id,
            tier="reject",
            model="",
            compute_budget_usd=0.0,
            expected_revenue_usd=expected_revenue_usd,
            expected_margin_usd=decision.expected_margin_usd,
            session_id=task_id or None,
        )
        db.log_audit(
            "system", "compute_tier_rejected",
            {**audit_payload, "tier": "reject"},
        )
        return {
            "action": "block",
            "message": f"Argus rejected compute: {decision.reason}",
            "verdict": decision.verdict,
            "expected_margin_usd": decision.expected_margin_usd,
        }

    if decision.needs_approval:
        # Mirror cash flow: enqueue approval, wait synchronously.
        req_id = db.create_approval_request(
            job_id=job_id,
            cost_center_id=cost_center_id,
            projected_usd=projected_burn_usd,
            level="manager",
            tool_name="argus_request_compute",
            ref=ref,
        )
        db.log_audit(
            "system", "compute_approval_requested",
            {**audit_payload, "approval_id": req_id},
        )
        status = _wait_for_decision(
            req_id, timeout=APPROVAL_TIMEOUT_SEC, poll=POLL_INTERVAL_SEC,
        )
        if status != "approved":
            db.log_audit("system", f"compute_{status}", {"approval_id": req_id})
            return {
                "action": "block",
                "message": f"Argus blocked compute ({status}): {decision.reason}",
            }
        db.log_audit("system", "compute_resumed", {"approval_id": req_id})

    # ALLOW path — issue an auth token bound to the assigned compute budget.
    token = db.issue_auth_token(
        job_id=job_id,
        cost_center_id=cost_center_id,
        amount_usd=decision.compute_budget_usd,
        ttl_seconds=AUTH_TOKEN_TTL_SEC,
        tolerance_pct=AUTH_TOKEN_TOLERANCE,
    )

    alloc_id = db.insert_compute_allocation(
        job_id=job_id,
        cost_center_id=cost_center_id,
        tier=decision.tier_label,
        model=decision.model,
        compute_budget_usd=decision.compute_budget_usd,
        expected_revenue_usd=expected_revenue_usd,
        expected_margin_usd=decision.expected_margin_usd,
        session_id=task_id or None,
        auth_token=token["auth_token"],
    )
    db.log_audit(
        "system", "compute_tier_assigned",
        {**audit_payload, "tier": decision.tier_label, "allocation_id": alloc_id,
         "compute_budget_usd": decision.compute_budget_usd},
    )

    return {
        "action": "allow",
        "verdict": decision.verdict,
        "tier": decision.tier_label,
        "model": decision.model,
        "compute_budget_usd": decision.compute_budget_usd,
        "expected_margin_usd": decision.expected_margin_usd,
        "auth_token": token["auth_token"],
        "expires_in": token["expires_in"],
        "allocation_id": alloc_id,
    }
