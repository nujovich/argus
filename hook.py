"""Argus Capture + Enforcement — the pre_tool_call hook.

Fires before every tool. When the tool is a spend-related call (Stripe skill
or the explicit argus_request_spend declaration), Argus reads the budget,
runs the pure policy, and either lets the tool proceed or creates an
approval request and **synchronously holds** until a human decides — see
CLAUDE.md §6 for why this shape.

Out-of-shape calls (anything not declared as a spend) are pass-through so
agents that don't opt in keep working.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import config as _cfg
import db
import policy


# Match anything that looks like an outbound Stripe spend. The explicit
# declaration skill ('argus_request_spend') is the documented path; the
# stripe_* prefix catches accidental direct calls so we audit them.
_DECL_TOOL = "argus_request_spend"
_STRIPE_PREFIX = "stripe_"


# How long the hook will block waiting for a human decision before
# auto-rejecting. Configurable via env later; demo default 5 min.
APPROVAL_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 0.5


def _is_spend_call(tool_name: str) -> bool:
    return tool_name == _DECL_TOOL or tool_name.startswith(_STRIPE_PREFIX)


def _extract_declaration(
    tool_name: str, args: Dict[str, Any]
) -> Optional[policy.SpendDeclaration]:
    """Pull (job_id, cost_center_id, projected_usd, ref) out of tool args.

    Returns None when the call isn't a declared spend — Argus then logs and
    lets it through, rather than breaking unrelated agents.
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


def _build_snapshot(decl: policy.SpendDeclaration) -> Tuple[policy.BudgetSnapshot, list]:
    budgets = _cfg.load_budgets()
    budget = budgets.get(decl.cost_center_id) or budgets.get("default")
    if budget is None:
        # No config at all — fall back to a permissive snapshot.
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
    # For the demo, attribute every job to the matched cost center directly.
    # Future: derive job→cc mapping from cost_centers.yaml.
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
    """Poll the approvals table until status leaves 'pending', or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = db.get_approval_status(req_id)
        if status and status != "pending":
            return status
        time.sleep(poll)
    db.mark_timeout(req_id)
    return "timeout"


def on_pre_tool_call(tool_name: str, args: Dict[str, Any], task_id: str, **_: Any):
    """Hermes pre_tool_call callback. Returns None to allow, dict to block."""
    if not _is_spend_call(tool_name):
        return None

    decl = _extract_declaration(tool_name, args or {})
    if decl is None:
        db.log_audit(
            "system",
            "spend_skipped_missing_declaration",
            {"tool": tool_name, "task_id": task_id},
        )
        return None

    snap, _ = _build_snapshot(decl)
    decision = policy.decide(decl, snap)

    audit_payload = {
        "tool": tool_name,
        "task_id": task_id,
        "job_id": decl.job_id,
        "cost_center_id": decl.cost_center_id,
        "projected_usd": decl.projected_usd,
        "verdict": decision.verdict,
        "reason": decision.reason,
    }
    db.log_audit("system", "spend_evaluated", audit_payload)

    if not decision.needs_approval:
        # The declaration tool is the gate; the actual stripe_* call that
        # follows is what moves money. For the demo path, record the intent.
        if tool_name == _DECL_TOOL:
            db.insert_ledger_row(
                job_id=decl.job_id,
                kind="external_spend",
                amount_usd=decl.projected_usd,
                source="argus_declaration",
                ref=decl.ref,
                session_id=task_id or None,
            )
        return None

    req_id = db.create_approval_request(
        job_id=decl.job_id,
        cost_center_id=decl.cost_center_id,
        projected_usd=decl.projected_usd,
        level=decision.level or "finance",
        tool_name=tool_name,
        ref=decl.ref,
    )
    db.log_audit(
        "system",
        "approval_requested",
        {**audit_payload, "approval_id": req_id, "level": decision.level},
    )

    status = _wait_for_decision(
        req_id, timeout=APPROVAL_TIMEOUT_SEC, poll=POLL_INTERVAL_SEC
    )

    if status == "approved":
        if tool_name == _DECL_TOOL:
            db.insert_ledger_row(
                job_id=decl.job_id,
                kind="external_spend",
                amount_usd=decl.projected_usd,
                source="argus_declaration",
                ref=decl.ref,
                session_id=task_id or None,
            )
        db.log_audit("system", "spend_resumed", {"approval_id": req_id})
        return None

    msg = f"Argus blocked spend ({status}): {decision.reason}"
    db.log_audit("system", f"spend_{status}", {"approval_id": req_id})
    return {"action": "block", "message": msg}
