"""Capture layer tests — the intent→confirmation loop (CLAUDE.md §2 Capture).

Capture writes FACTS to the ledger: it records the REAL confirmed spend in a
post_tool_call recorder, keeps the attribution chain durable, and (under A1)
relies on the store's telemetry ATTACH for llm_cost — it never re-measures it.

post_tool_call payload shape is the Hermes ground truth (model_tools.
_emit_post_tool_call_hook): top-level kwargs tool_name / args / result /
status / session_id / task_id / tool_call_id — NOT nested under `extra`.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

import capture
import config as _cfg
import db
import enforcement


# ── helpers ──────────────────────────────────────────────────────────────────


def _term(command):
    return {"command": command}


def _seed_default_cc(auto=0.50, manager=5.0, limit=50.0):
    db.init_db()
    db.upsert_cost_center("default", "Default")
    db.upsert_budget("default", limit_usd=limit, soft_threshold=0.8,
                     auto_approve_under_usd=auto, manager_under_usd=manager)


def _events():
    return [a["event"] for a in db.get_recent_audit(100)]


def _external_rows():
    conn = db._get_conn()
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM ledger WHERE kind='external_spend' ORDER BY id"
        ).fetchall()
    ]


# ── C) post_tool_call confirmed-spend recorder ──────────────────────────────


def test_success_parseable_writes_real_amount_and_ref(tmp_hermes_home):
    _seed_default_cc()
    capture.request_spend("jP", 0.25, session_id="sP", cost_center_id="default")

    capture.on_post_tool_call(
        tool_name="terminal",
        args=_term("mpp pay --amount 0.25"),
        result=json.dumps({"output": "Payment captured pi_3MAbc123Def456 for $0.30"}),
        status="ok",
        session_id="sP",
        tool_call_id="tc-1",
    )

    rows = _external_rows()
    assert len(rows) == 1
    assert rows[0]["amount_usd"] == 0.30          # the REAL captured amount, not 0.25
    assert rows[0]["ref"] == "pi_3MAbc123Def456"
    assert rows[0]["source"] == "stripe"
    assert rows[0]["job_id"] == "jP"

    # Declaration consumed; audit recorded the confirmation (not estimated).
    assert db.find_open_declaration(session_id="sP") is None
    assert "spend_confirmed" in _events()
    assert "spend_confirmed_estimated" not in _events()


def test_success_unparseable_falls_back_to_projection_flagged(tmp_hermes_home):
    _seed_default_cc()
    capture.request_spend("jU", 7.0, session_id="sU", cost_center_id="default")

    capture.on_post_tool_call(
        tool_name="terminal",
        args=_term("stripe projects add openai/gpt-4o"),
        result="provisioned successfully (no machine-readable amount here)",
        status="ok",
        session_id="sU",
        tool_call_id="tc-2",
    )

    rows = _external_rows()
    # NEVER dropped — under-counting spend overstates P&L (the dangerous way).
    assert len(rows) == 1
    assert rows[0]["amount_usd"] == 7.0           # the gating projection
    assert "spend_confirmed_estimated" in _events()
    confirmed = [a for a in db.get_recent_audit(100)
                 if a["event"] == "spend_confirmed_estimated"][0]
    assert confirmed["payload"]["estimated"] is True


def test_failure_writes_no_row_and_audits(tmp_hermes_home):
    _seed_default_cc()
    capture.request_spend("jF", 3.0, session_id="sF", cost_center_id="default")

    capture.on_post_tool_call(
        tool_name="terminal",
        args=_term("mpp pay --amount 3.00"),
        result=json.dumps({"error": "card_declined"}),
        status="error",
        session_id="sF",
        tool_call_id="tc-3",
    )

    assert _external_rows() == []                 # no money moved
    assert "spend_failed" in _events()
    assert "spend_confirmed" not in _events()
    # The declaration is NOT consumed on failure (the agent may retry).
    assert db.find_open_declaration(session_id="sF") is not None


def test_idempotent_same_tool_call_id_writes_one_row(tmp_hermes_home):
    _seed_default_cc()
    capture.request_spend("jI", 5.0, session_id="sI", cost_center_id="default")

    kwargs = dict(
        tool_name="terminal",
        args=_term("mpp pay --amount 5.00"),
        result=json.dumps({"output": "ok pi_dupe123456 $5.00"}),
        status="ok",
        session_id="sI",
        tool_call_id="tc-dup",
    )
    capture.on_post_tool_call(**kwargs)
    capture.on_post_tool_call(**kwargs)            # replay

    assert len(_external_rows()) == 1


def test_non_spend_command_is_ignored(tmp_hermes_home):
    _seed_default_cc()
    capture.on_post_tool_call(
        tool_name="terminal",
        args=_term("ls -la"),
        result="files",
        status="ok",
        session_id="sN",
        tool_call_id="tc-4",
    )
    assert _external_rows() == []
    assert "spend_confirmed" not in _events()


# ── B) attribution chain resolves from yaml, not defaults (§9 dec 3) ─────────


def test_attribution_chain_resolves_from_yaml(tmp_hermes_home):
    argus = tmp_hermes_home / "argus"
    argus.mkdir(parents=True, exist_ok=True)
    (argus / "cost_centers.yaml").write_text(textwrap.dedent("""
        cost_centers:
          saas: {label: SaaS, limit_usd: 200.0, auto_approve_under_usd: 0.0}
        jobs:
          job-saas: saas
    """))
    db.init_db()

    capture.ensure_attribution("job-saas", session_id="sess-saas")

    # session → job → cost_center, resolved through the bridge + config.
    assert db.get_job_for_session("sess-saas") == "job-saas"
    assert db.get_cost_center_for_job("job-saas") == "saas"   # yaml, NOT "default"


# ── D) A1: Capture only links sessions; llm_cost is DERIVED by the store ─────


def _make_fixture_telemetry(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tconn = sqlite3.connect(str(path))
    tconn.execute(
        "CREATE TABLE runs (session_id TEXT PRIMARY KEY, model TEXT,"
        " cost_usd REAL, started_at TEXT)"
    )
    tconn.executemany(
        "INSERT INTO runs(session_id, model, cost_usd, started_at) VALUES(?,?,?,?)",
        rows,
    )
    tconn.commit()
    tconn.close()


def test_a1_llm_cost_attributes_via_capture_link(tmp_hermes_home):
    db.init_db()
    # Capture's only job for llm_cost: ensure the job_sessions link exists.
    capture.ensure_attribution("jobLLM", session_id="sessLLM", cost_center_id="default")
    db.append_fact("jobLLM", "revenue", 200.0)

    _make_fixture_telemetry(_cfg.telemetry_db_path(), [
        ("sessLLM", "nvidia/nemotron-3-ultra-550b-a55b", 15.0, "t"),
    ])

    pnl = {r["job_id"]: r for r in db.pnl_by_job()}
    assert pnl["jobLLM"]["llm_cost"] == 15.0       # derived by the ATTACH join
    assert pnl["jobLLM"]["pnl"] == 185.0           # 200 - 15, no re-measurement


# ── intent → confirmation, end to end (simulated, no live Hermes) ────────────


def test_intent_to_confirmation_end_to_end(tmp_hermes_home):
    _seed_default_cc(auto=0.50)

    # 1. INTENT — agent declares the spend (durable). projected $0.25.
    capture.request_spend("jE2E", 0.25, session_id="sE2E", cost_center_id="default")

    # 2. GATE — pre_tool_call sees the open declaration, auto-approves (≤ $0.50).
    gate = enforcement.on_pre_tool_call(
        "terminal", _term("mpp pay --amount 0.25"), "sE2E"
    )
    assert gate is None                            # ALLOW → Hermes runs the command

    # 3. CONFIRMATION — post_tool_call records the REAL captured amount ($0.30).
    capture.on_post_tool_call(
        tool_name="terminal",
        args=_term("mpp pay --amount 0.25"),
        result=json.dumps({"output": "captured pi_e2e_987654 $0.30"}),
        status="ok",
        session_id="sE2E",
        tool_call_id="tc-e2e",
    )

    # P&L reflects the CONFIRMED amount, not the projection.
    pnl = {r["job_id"]: r for r in db.pnl_by_job()}
    assert pnl["jE2E"]["external_spend"] == 0.30   # confirmed, not 0.25
    assert pnl["jE2E"]["pnl"] == -0.30

    # Full audit lifecycle: attempted → approved → confirmed.
    events = _events()
    assert "spend_attempted" in events
    assert "spend_approved" in events
    assert "spend_confirmed" in events
