"""Argus Enforcement — the pre_tool_call hook that gates REAL Stripe spend.

CLAUDE.md §2 (Enforcement: the only layer that writes runtime state for cash
decisions), §3 (HITL tiers + synchronous hold), §6 (validated Path A in-process
poll), §9.1(c) (projected_usd via a request_spend declaration).

GROUND TRUTH (corrects §4 wording — see MIGRATION_NOTES): the Stripe skills do
NOT register their own tools. Every command runs through the `terminal` tool, so
the hook payload is {tool_name:"terminal", tool_input:{command:"..."}}. Enforcement
therefore matches tool_name=="terminal" AND a spend COMMAND pattern — not
`stripe_*` tool names. §4's "matches tool names" should read "matches terminal
commands".

FAIL-CLOSED is the invariant that defines a financial gate (§6): Hermes fails
OPEN if a hook raises (tool_executor.py:283), so on a matched spend command this
layer wraps everything in try/except and returns BLOCK on ANY error. There is no
path on a matched spend command that returns allow without an explicit Policy
ALLOW or a human approval.

Reuses the Ledger store (db) and the PURE Policy (policy.evaluate_spend)
unchanged — Enforcement is the composer/caller, never reimplements them, and
never lets Policy touch I/O.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

import db
import policy


# ── tunables ────────────────────────────────────────────────────────────────
APPROVAL_TIMEOUT_SEC = 300        # treat as rejection past this — never resume
POLL_INTERVAL_SEC = 0.5           # validated §6 Path A poll cadence
DEFAULT_COST_CENTER = "default"


# ── spend command matchers ───────────────────────────────────────────────────
# Gate ONLY real spend. Patterns are deliberately specific: over-matching blocks
# the agent on reads; under-matching leaks spend. `list`/`catalog`/`status`/`init`
# never match these, so they pass through untouched.
_SPEND_PATTERNS = (
    re.compile(r"\bstripe\s+projects\s+add\b"),       # provisioning spend
    re.compile(r"\bstripe\s+projects\s+upgrade\b"),   # tier change = spend
    re.compile(r"\bmpp\s+pay\b"),                      # link-cli / 402 pay path
)


def _command_of(tool_name: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the shell command string iff this is a `terminal` tool call."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    cmd = args.get("command")
    if cmd is None:
        cmd = args.get("cmd")  # tolerate alternate key
    return str(cmd) if cmd is not None else None


def is_spend_command(command: str) -> bool:
    return any(p.search(command) for p in _SPEND_PATTERNS)


# ── request_spend declaration correlation (§9.1(c)) ──────────────────────────
# In-process correlation cache: a request_spend(...) declaration the agent makes
# BEFORE its terminal spend command. Keyed by session/task id (the correlation
# key carried in the hook payload).
#
# GAP (flagged, not built here): durable / cross-process declarations belong in
# the Ledger store as a table written by Capture; this in-process cache is the
# enforcement-side correlation only. Recording declarations is Capture's job.
_declarations: Dict[str, Dict[str, Any]] = {}
_decl_lock = threading.Lock()


def declare_spend(
    job_id: str,
    projected_usd: float,
    *,
    cost_center_id: str = DEFAULT_COST_CENTER,
    ref: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a request_spend declaration for later correlation. Returns the
    stored record. (Called by Capture / the request_spend skill; exposed here
    so Enforcement can correlate without reaching into another layer.)"""
    rec = {
        "job_id": str(job_id),
        "cost_center_id": str(cost_center_id),
        "projected_usd": round(float(projected_usd), 2),
        "ref": ref,
        "session_id": session_id,
    }
    with _decl_lock:
        if session_id:
            _declarations[str(session_id)] = rec
        _declarations[f"job:{job_id}"] = rec
    return rec


def _lookup_declaration(session_id: str, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with _decl_lock:
        if session_id and session_id in _declarations:
            return _declarations[session_id]
        if job_id and f"job:{job_id}" in _declarations:
            return _declarations[f"job:{job_id}"]
    return None


def clear_declarations() -> None:
    """Test/reset hook."""
    with _decl_lock:
        _declarations.clear()


# ── projected_usd resolution (priority order — never guess low) ──────────────
_AMOUNT_PATTERNS = (
    re.compile(r"--amount(?:[-_]usd)?[ =]\$?(\d+(?:\.\d{1,2})?)"),
    re.compile(r"\$(\d+(?:\.\d{1,2})?)"),
)


def _parse_amount(command: str) -> Optional[float]:
    """Parse an explicit dollar amount from a spend command (mpp pay / 402
    challenge). Conservative: only explicit `--amount`/`$` forms — anything
    ambiguous returns None so the caller forces approval rather than guessing low."""
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(command)
        if m:
            return round(float(m.group(1)), 2)
    return None


def _resolve_projected(
    decl: Optional[Dict[str, Any]], command: str
) -> Tuple[Optional[float], str]:
    if decl is not None and decl.get("projected_usd") is not None:
        return float(decl["projected_usd"]), "declaration"
    amt = _parse_amount(command)
    if amt is not None:
        return amt, "parsed"
    return None, "unknown"


# ── snapshot composition (the gap Policy flagged — Enforcement composes it) ──
def _compose_snapshot(cost_center_id: str, job_id: str) -> policy.SpendSnapshot:
    """Build the SpendSnapshot Policy needs from ledger_snapshot() + budget_for()
    + per-job revenue/spend from pnl_by_job(). Reads the store; never writes."""
    snap = db.ledger_snapshot(cost_center_id)          # {spent_usd, limit_usd, soft_threshold, ...}
    budget_row = db.budget_for(cost_center_id)          # budgets row or None

    if budget_row is not None:
        limits = policy.BudgetLimits(
            limit_usd=float(budget_row["limit_usd"]),
            soft_threshold=float(budget_row["soft_threshold"]),
            auto_approve_under_usd=float(budget_row["auto_approve_under_usd"]),
            manager_under_usd=(
                float(budget_row["manager_under_usd"])
                if budget_row.get("manager_under_usd") is not None else None
            ),
        )
    else:
        # No budget configured → fail safe: nothing auto-approves, no manager
        # tier, zero limit so any positive spend breaches → finance.
        limits = policy.BudgetLimits(
            limit_usd=0.0, soft_threshold=0.8,
            auto_approve_under_usd=0.0, manager_under_usd=None,
        )

    pnl = {r["job_id"]: r for r in db.pnl_by_job()}
    row = pnl.get(job_id)
    job_revenue = float(row["revenue"]) if row else None
    job_spend = float(row["llm_cost"] + row["external_spend"]) if row else None

    return policy.SpendSnapshot(
        cost_center_id=cost_center_id,
        budget=limits,
        cost_center_used_usd=float(snap.get("spent_usd") or 0.0),
        job_revenue_usd=job_revenue,
        job_spend_so_far_usd=job_spend,
    )


# ── the hold poll (validated §6 Path A; WAL-robust reads via db) ─────────────
def _wait_for_decision(req_id: str, *, timeout: float, poll: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = db.get_approval_status(req_id)   # WAL-robust read (db layer)
        if status and status != "pending":
            return status
        time.sleep(poll)
    db.mark_timeout(req_id)
    return "timeout"


_BLOCK_REJECTED = "rejected"
_BLOCK_TIMEOUT = "timeout"


def _block(message: str) -> Dict[str, str]:
    return {"action": "block", "message": message}


# ── the gate (runs only for a matched spend command; fail-closed wrapper) ────
def _gate_spend_command(command: str, session_id: str) -> Optional[Dict[str, Any]]:
    decl = _lookup_declaration(session_id)
    if decl is not None:
        job_id = decl["job_id"]
        cost_center_id = decl["cost_center_id"]
    else:
        # No declaration: attribute to the session, default cost center.
        # GAP (flagged): resolving cost_center from a previously-registered job
        # via the jobs/job_sessions bridge would need read getters
        # (get_job_for_session / get_cost_center_for_job) the store does not
        # expose. Until then, cc comes from the declaration or defaults here.
        job_id = f"session:{session_id}" if session_id else "unknown-job"
        cost_center_id = DEFAULT_COST_CENTER

    projected, source = _resolve_projected(decl, command)

    db.append_audit(
        "agent", "spend_attempted",
        {
            "command": command, "job_id": job_id, "cost_center_id": cost_center_id,
            "projected_usd": projected if projected is not None else "unknown",
            "amount_source": source,
        },
    )

    # Ensure the job exists (idempotent), then link the session for attribution.
    # link_session has an FK to jobs(job_id); register_job upserts the job + its
    # cost center first. Enforcement may write runtime state (§2).
    db.register_job(job_id, cost_center_id)
    if session_id:
        db.link_session(session_id, job_id)

    # Decide tier + reason.
    if projected is None:
        # Undeclared spend NEVER auto-approves — force finance approval.
        tier = "finance"
        reason = "spend amount undeclared"
        projected_row = 0.0
    else:
        snapshot = _compose_snapshot(cost_center_id, job_id)
        verdict = policy.evaluate_spend(job_id, cost_center_id, projected, snapshot)
        if verdict.allowed:
            db.append_audit(
                "system", "spend_approved",
                {"mode": "auto", "job_id": job_id, "cost_center_id": cost_center_id,
                 "projected_usd": projected, "reason": verdict.reason},
            )
            return None  # ALLOW → Hermes runs the terminal command (resume/no-op)
        tier = verdict.tier or "finance"
        reason = verdict.reason
        projected_row = projected

    # NEEDS_APPROVAL → enqueue + synchronously hold (Path A).
    req_id = db.create_approval(job_id, cost_center_id, projected_row, level=tier)
    db.append_audit(
        "system", "approval_requested",
        {"approval_id": req_id, "tier": tier, "reason": reason, "job_id": job_id,
         "cost_center_id": cost_center_id,
         "projected_usd": projected if projected is not None else "unknown"},
    )

    status = _wait_for_decision(
        req_id, timeout=APPROVAL_TIMEOUT_SEC, poll=POLL_INTERVAL_SEC
    )

    if status == "approved":
        row = db.read_approval(req_id) or {}
        db.append_audit(
            "system", "spend_approved",
            {"mode": "human", "approval_id": req_id, "decided_by": row.get("decided_by"),
             "job_id": job_id},
        )
        return None  # resume: Hermes executes the held command as if a no-op

    if status == _BLOCK_REJECTED:
        row = db.read_approval(req_id) or {}
        db.append_audit(
            "system", "spend_rejected",
            {"approval_id": req_id, "decided_by": row.get("decided_by")},
        )
        return _block(f"Argus blocked spend (rejected): {row.get('reason') or reason}")

    # timeout (or any non-approved terminal state) → block. Never resume.
    db.append_audit("system", "spend_timeout", {"approval_id": req_id, "reason": reason})
    return _block(f"Argus blocked spend (timeout): {reason}")


def on_pre_tool_call(
    tool_name: str = "", args: Any = None, task_id: str = "", **kwargs: Any
) -> Optional[Dict[str, Any]]:
    """Hermes pre_tool_call callback. None → allow; dict → block.

    Only `terminal` calls carrying a spend command are gated. Everything else
    passes through untouched. A matched spend command runs inside a fail-closed
    wrapper: ANY exception returns BLOCK (Hermes would otherwise fail OPEN)."""
    command = _command_of(tool_name, args if isinstance(args, dict) else None)
    if command is None or not is_spend_command(command):
        return None  # not a gated spend — allow (correct: not Enforcement's concern)

    session_id = str(kwargs.get("session_id") or task_id or "")
    try:
        return _gate_spend_command(command, session_id)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on anything
        try:
            db.append_audit(
                "system", "enforcement_error_blocked",
                {"command": command, "session_id": session_id, "error": repr(exc)},
            )
        except Exception:
            pass  # even audit failed — still block
        return _block(
            "Argus blocked spend: internal enforcement error (failing closed)."
        )


def register(ctx) -> None:
    """Register the spend-enforcement pre_tool_call hook. Minimal by design."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
