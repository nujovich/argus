"""Argus Capture — instrumentation that writes FACTS to the Ledger.

CLAUDE.md §2 (Capture row): "writes revenue / llm_cost / external_spend to the
Ledger; reads telemetry read-only." Capture does two jobs:

 1. Makes the spend-declaration + attribution chain DURABLE — the intent half of
    the loop (request_spend) and the lazy session→job→cost_center links.
 2. Closes the intent→confirmation loop with a ``post_tool_call`` recorder that
    writes the REAL confirmed spend after the terminal command actually runs.

GROUND TRUTH — the ``post_tool_call`` payload (Hermes ``model_tools.
_emit_post_tool_call_hook``) passes everything as TOP-LEVEL kwargs:
``tool_name, args, result, task_id, session_id, tool_call_id, turn_id,
api_request_id, duration_ms, status, error_type, error_message``. They are NOT
nested under an ``extra`` object (the build brief's "extra.tool_call_id" wording
is corrected here; CLAUDE.md §11: the Hermes example wins). ``status`` is "ok"
on success, "error"/"cancelled" otherwise; ``result`` is the tool's return value
(for ``terminal``, a string — often a JSON envelope).

``on_session_start`` is NOT a wired hook, so ALL linking is done lazily inside
the wired ``post_tool_call`` hook (and the request_spend intake).

ATTRIBUTION-ONLY for llm_cost (A1): under the §5 read-only ATTACH, llm_cost is
DERIVED by the store's telemetry join at query time — Capture never re-measures
it. Capture's only llm_cost responsibility is keeping the job_sessions link
present so telemetry cost attributes to the right job. llm_cost rows are written
ONLY under the A2 (no-telemetry) fallback (not exercised here).

Revenue is OUT OF SCOPE for this hook layer — per §9.2 it enters via a
plugin_api.py Stripe webhook / sim path. It is the one remaining P&L input.

Shares the spend matcher with Enforcement (matchers.py) so the gate and the
recorder can never disagree about what a spend is.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import config as _cfg
import db
import matchers


# Shared matcher — single source of truth (CHANGE 1). Re-exported so callers and
# tests can reach the SAME function objects the gate uses.
is_spend_command = matchers.is_spend_command
command_of = matchers.command_of

DEFAULT_COST_CENTER = "default"


# ── A) Declaration intake (the intent half) ──────────────────────────────────


def request_spend(
    job_id: str,
    projected_usd: float,
    ref: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    cost_center_id: Optional[str] = None,
) -> int:
    """Durable spend declaration intake. Resolves the cost center from config
    (§9 decision 3), ensures the attribution chain, and writes a
    spend_declarations row. Returns the declaration id.

    This replaces Enforcement's old in-process correlation cache: the gate later
    reads the open declaration via db.find_open_declaration."""
    job_id = str(job_id)
    cc = cost_center_id or _cfg.cost_center_for_job(job_id)
    ensure_attribution(job_id, session_id, cost_center_id=cc)
    decl_id = db.insert_declaration(
        job_id=job_id,
        session_id=session_id,
        projected_usd=float(projected_usd),
        ref=ref,
    )
    db.append_audit(
        "agent", "spend_declared",
        {"job_id": job_id, "session_id": session_id,
         "projected_usd": round(float(projected_usd), 2), "ref": ref,
         "cost_center_id": cc, "declaration_id": decl_id},
    )
    return decl_id


# ── B) Attribution — lazy, idempotent session→job→cost_center links ──────────


def ensure_attribution(
    job_id: str,
    session_id: Optional[str] = None,
    *,
    cost_center_id: Optional[str] = None,
) -> str:
    """Ensure the job is registered to its cost center and (if given) the
    session is linked to the job. Idempotent; safe to call on every hook.

    cost_center resolves from config per §9 decision 3 (NOT a blind default)
    unless the caller supplies one. Returns the resolved cost center."""
    job_id = str(job_id)
    cc = cost_center_id or _cfg.cost_center_for_job(job_id)
    db.register_job(job_id, cc)
    if session_id:
        db.link_session(str(session_id), job_id)
    return cc


# ── confirmed-spend parsing (REAL amount + ref from the command output) ──────

# A stripe object / resource id: pi_, py_, ch_, in_, sub_, seti_, cs_, re_,
# prod_, price_, proj_, svc_, … — a short prefix then base62.
_REF_RE = re.compile(
    r"\b((?:pi|py|ch|in|sub|seti|cs|re|prod|price|proj|prj|svc|sub|acct|src|pm)_[A-Za-z0-9]{6,})\b"
)

# Dollar amounts, most explicit first. The Stripe-cents form (`"amount": 1250`)
# is divided by 100; everything else is already dollars.
_DOLLAR_RES = (
    re.compile(r"--amount(?:[-_]usd)?[ =]\$?(\d+(?:\.\d{1,2})?)"),
    re.compile(r"amount[_-]usd[\"']?\s*[:=]\s*\$?(\d+(?:\.\d{1,2})?)"),
    re.compile(r"\$(\d+(?:\.\d{1,2})?)"),
)
_CENTS_RE = re.compile(r"[\"']?amount[\"']?\s*[:=]\s*(\d{3,})\b")


def _result_text(result: Any) -> str:
    """Coerce the post_tool_call ``result`` into searchable text. Terminal
    results are typically a JSON string (sometimes a dict envelope); fall back
    to the raw value so a plain-stdout string still gets scanned."""
    if result is None:
        return ""
    if isinstance(result, dict):
        return _text_from_obj(result)
    if isinstance(result, str):
        try:
            obj = json.loads(result)
        except Exception:
            return result
        text = _text_from_obj(obj)
        return text or result
    return str(result)


def _text_from_obj(obj: Any) -> str:
    if isinstance(obj, dict):
        for k in ("output", "stdout", "result", "message", "text"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        try:
            return json.dumps(obj)
        except Exception:
            return ""
    if isinstance(obj, str):
        return obj
    return ""


def _parse_amount(text: str) -> Optional[float]:
    """Parse a confirmed dollar amount from output (or a command). Conservative:
    only explicit dollar / amount_usd / Stripe-cents forms; ambiguous → None so
    the caller falls back to the gating projection rather than guessing low."""
    if not text:
        return None
    for pat in _DOLLAR_RES:
        m = pat.search(text)
        if m:
            return round(float(m.group(1)), 2)
    m = _CENTS_RE.search(text)
    if m:
        return round(int(m.group(1)) / 100.0, 2)
    return None


def _parse_ref(text: str) -> Optional[str]:
    if not text:
        return None
    m = _REF_RE.search(text)
    return m.group(1) if m else None


def _succeeded(status: Optional[str], result: Any) -> bool:
    """A terminal command succeeded iff status == 'ok' (the Hermes-derived
    field). When status is absent, fall back to inspecting the result for an
    error envelope (mirrors model_tools._tool_result_observer_fields)."""
    if status:
        return status == "ok"
    return not _result_is_error(result)


def _result_is_error(result: Any) -> bool:
    try:
        obj = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return False
    if isinstance(obj, dict):
        if obj.get("error"):
            return True
        if obj.get("status") in ("error", "cancelled", "failed"):
            return True
    return False


# ── C) post_tool_call confirmed-spend recorder (the headline) ────────────────


def on_post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    status: str = "",
    **kwargs: Any,
) -> None:
    """Observer hook. Records the REAL confirmed external spend after a matched
    terminal spend command runs. Never blocks (post_tool_call is observational);
    swallows its own errors so it can never break the agent loop.

    Matches via the SHARED matcher, correlates to the open declaration via the
    session, and:
      - SUCCEEDED + parseable amount → external_spend row with the REAL amount
        and ref, consume the declaration, audit ``spend_confirmed``.
      - SUCCEEDED + unparseable      → row using the gating projection, flagged
        estimated, audit ``spend_confirmed_estimated``. NEVER drop a confirmed
        spend (under-counting overstates P&L — the dangerous direction).
      - FAILED                       → no ledger row, audit ``spend_failed``.
      - replay (same tool_call_id)   → no-op (idempotent)."""
    try:
        command = command_of(tool_name, args if isinstance(args, dict) else None)
        if command is None or not is_spend_command(command):
            return None  # not a gated spend — nothing to record

        # Idempotency: a confirmed spend is recorded at most once per tool call.
        if tool_call_id and db.external_spend_recorded(tool_call_id):
            return None

        sid = str(session_id or task_id or "")
        decl = db.find_open_declaration(session_id=sid) if sid else None
        if decl is not None:
            job_id = decl["job_id"]
        else:
            job_id = (db.get_job_for_session(sid) if sid else None) or (
                f"session:{sid}" if sid else "unknown-job"
            )

        # Keep the attribution chain present (lazy; on_session_start isn't wired).
        ensure_attribution(job_id, sid or None)

        if not _succeeded(status, result):
            # No money moved — do NOT consume the declaration (the agent may retry).
            db.append_audit(
                "agent", "spend_failed",
                {"command": command, "job_id": job_id, "session_id": sid,
                 "tool_call_id": tool_call_id, "status": status},
            )
            return None

        text = _result_text(result)
        amount = _parse_amount(text)
        if amount is None:
            amount = _parse_amount(command)  # the command may carry --amount/$
        ref = _parse_ref(text) or _parse_ref(command)

        estimated = False
        if amount is None:
            # SUCCEEDED but unparseable → conservative fallback to the gating
            # projection. Never drop a confirmed spend.
            amount = float(decl["projected_usd"]) if decl else 0.0
            estimated = True

        db.insert_ledger_row(
            job_id=job_id,
            kind="external_spend",
            amount_usd=amount,
            source="stripe",
            ref=ref,
            session_id=sid or None,
            tool_call_id=tool_call_id or None,
        )
        if decl is not None:
            db.mark_declaration_consumed(decl["id"])

        db.append_audit(
            "agent",
            "spend_confirmed_estimated" if estimated else "spend_confirmed",
            {"command": command, "job_id": job_id, "amount_usd": amount,
             "ref": ref, "session_id": sid, "tool_call_id": tool_call_id,
             "estimated": estimated,
             "declaration_id": decl["id"] if decl else None},
        )
        return None
    except Exception as exc:  # noqa: BLE001 — an observer must never break the loop
        try:
            db.append_audit(
                "system", "capture_error",
                {"tool_name": tool_name, "tool_call_id": tool_call_id, "error": repr(exc)},
            )
        except Exception:
            pass
        return None


# ── D) llm_cost (A2 fallback only) ───────────────────────────────────────────


def record_llm_cost_fallback(
    job_id: str, amount_usd: float, *, session_id: Optional[str] = None,
    ref: Optional[str] = None,
) -> int:
    """A2 ONLY: when hermes-telemetry is unavailable, llm_cost can't be derived
    by the ATTACH join, so Capture writes an explicit llm_cost fact. Under A1
    (telemetry present) this is NOT called — llm_cost is derived at query time."""
    return db.insert_ledger_row(
        job_id=str(job_id), kind="llm_cost", amount_usd=float(amount_usd),
        source="argus_a2_fallback", ref=ref, session_id=session_id,
    )


def register(ctx) -> None:
    """Register the Capture post_tool_call recorder. Enforcement keeps its own
    pre_tool_call registration; the request_spend intake is a module function
    invoked by the plugin API / skill (not a hook event)."""
    ctx.register_hook("post_tool_call", on_post_tool_call)
