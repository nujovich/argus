"""Revenue intake tests (CLAUDE.md §9.2) — sim + real Stripe webhook, the
three-sided P&L close, treasury, and the demo-as-a-test end-to-end.

Revenue enters via plugin_api.py HTTP (NOT a hook). These tests use FastAPI's
TestClient and stdlib-HMAC Stripe signatures — no live Stripe, no `stripe` lib.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import capture
import config as _cfg
import db
import enforcement


SECRET = "whsec_test_secret_123"


def _client() -> TestClient:
    from dashboard import plugin_api  # noqa: WPS433

    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


# ── Stripe signature helpers (the real scheme: t=<ts>,v1=<hmac_sha256>) ──────


def _sign(body: bytes, secret: str, t: int | None = None) -> str:
    t = t if t is not None else int(time.time())
    signed = f"{t}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _post_webhook(client, payload: dict, secret: str = SECRET,
                  sign: bool = True, t: int | None = None):
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json"}
    if sign:
        headers["stripe-signature"] = _sign(body, secret, t)
    return client.post("/revenue/stripe", content=body, headers=headers)


def _events():
    return [a["event"] for a in db.get_recent_audit(200)]


def _revenue_rows():
    conn = db._get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ledger WHERE kind='revenue' ORDER BY id"
    ).fetchall()]


# ── A) SIM endpoint ──────────────────────────────────────────────────────────


def test_sim_records_revenue_for_job(tmp_hermes_home):
    c = _client()
    r = c.post("/revenue/sim", json={"job_id": "jA", "amount_usd": 25.0, "ref": "pi_a"})
    assert r.status_code == 200
    assert r.json()["recorded"] == "revenue"
    rows = _revenue_rows()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "jA" and rows[0]["amount_usd"] == 25.0
    assert rows[0]["source"] == "stripe-sim"
    assert "revenue_received" in _events()


def test_sim_idempotent_on_ref(tmp_hermes_home):
    c = _client()
    body = {"job_id": "jA", "amount_usd": 25.0, "ref": "pi_dupe"}
    c.post("/revenue/sim", json=body)
    c.post("/revenue/sim", json=body)
    assert len(_revenue_rows()) == 1


def test_sim_missing_job_id_is_400(tmp_hermes_home):
    c = _client()
    r = c.post("/revenue/sim", json={"amount_usd": 10.0, "ref": "pi_x"})
    assert r.status_code == 400
    assert _revenue_rows() == []


# ── B) REAL Stripe webhook (signature verification is mandatory) ─────────────


def _pi_event(amount_cents=4200, job_id="j-stripe", evt_id="evt_1", pi_id="pi_real_1"):
    return {
        "id": evt_id,
        "type": "payment_intent.succeeded",
        "data": {"object": {
            "id": pi_id, "object": "payment_intent",
            "amount_received": amount_cents, "currency": "usd",
            "status": "succeeded",
            "metadata": {"job_id": job_id} if job_id else {},
        }},
    }


def test_webhook_valid_signature_with_job_id(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_STRIPE_WEBHOOK_SECRET", SECRET)
    c = _client()
    r = _post_webhook(c, _pi_event())
    assert r.status_code == 200
    assert r.json()["recorded"] == "revenue"
    rows = {x["job_id"]: x for x in _revenue_rows()}
    assert rows["j-stripe"]["amount_usd"] == 42.0
    assert rows["j-stripe"]["ref"] == "pi_real_1"


def test_webhook_invalid_signature_is_400_no_row(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_STRIPE_WEBHOOK_SECRET", SECRET)
    c = _client()
    # Sign with the WRONG secret → verification must fail.
    r = _post_webhook(c, _pi_event(), secret="whsec_WRONG")
    assert r.status_code == 400
    assert _revenue_rows() == []


def test_webhook_missing_signature_is_400(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_STRIPE_WEBHOOK_SECRET", SECRET)
    c = _client()
    r = _post_webhook(c, _pi_event(), sign=False)
    assert r.status_code == 400
    assert _revenue_rows() == []


def test_webhook_duplicate_event_one_row(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_STRIPE_WEBHOOK_SECRET", SECRET)
    c = _client()
    _post_webhook(c, _pi_event())
    r2 = _post_webhook(c, _pi_event())          # same pi id → duplicate delivery
    assert r2.status_code == 200
    assert len(_revenue_rows()) == 1


def test_webhook_checkout_session_completed(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_STRIPE_WEBHOOK_SECRET", SECRET)
    c = _client()
    evt = {
        "id": "evt_cs", "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_1", "amount_total": 12000,
            "metadata": {"job_id": "j-checkout"},
        }},
    }
    r = _post_webhook(c, evt)
    assert r.status_code == 200
    rows = {x["job_id"]: x for x in _revenue_rows()}
    assert rows["j-checkout"]["amount_usd"] == 120.0


def test_webhook_missing_job_id_goes_unattributed(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_STRIPE_WEBHOOK_SECRET", SECRET)
    c = _client()
    r = _post_webhook(c, _pi_event(job_id=None, evt_id="evt_u", pi_id="pi_unattr"))
    assert r.status_code == 200
    rows = {x["job_id"]: x for x in _revenue_rows()}
    # Recorded to the sentinel — NOT guessed onto a real job.
    assert "unattributed" in rows
    assert rows["unattributed"]["amount_usd"] == 42.0
    assert "revenue_unattributed" in _events()
    # Treasury still correct (cash counts the unattributed revenue).
    assert db.cash_position() == 42.0


# ── three-sided P&L: revenue − llm_cost(A1 telemetry) − external_spend ───────


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


def test_three_sided_pnl_via_api(tmp_hermes_home):
    db.init_db()
    db.register_job("j3", "default")
    db.link_session("sess3", "j3")
    db.append_fact("j3", "external_spend", 4.0, source="stripe")
    _make_fixture_telemetry(_cfg.telemetry_db_path(),
                            [("sess3", "nvidia/nemotron-3-ultra-550b-a55b", 6.0, "t")])

    c = _client()
    c.post("/revenue/sim", json={"job_id": "j3", "amount_usd": 20.0, "ref": "pi_3"})

    pnl = {r["job_id"]: r for r in c.get("/pnl").json()["jobs"]}
    assert pnl["j3"]["revenue"] == 20.0
    assert pnl["j3"]["llm_cost"] == 6.0          # derived via A1 ATTACH
    assert pnl["j3"]["external_spend"] == 4.0
    assert pnl["j3"]["pnl"] == 10.0              # 20 - 6 - 4


# ── treasury ─────────────────────────────────────────────────────────────────


def test_treasury_breakdown(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_SEED_CAPITAL", "100.0")
    db.init_db()
    db.append_fact("jt", "revenue", 50.0, source="stripe")
    db.append_fact("jt", "llm_cost", 5.0)
    db.append_fact("jt", "external_spend", 12.0, source="stripe")

    assert db.cash_position() == 133.0           # 100 + 50 - 5 - 12

    t = _client().get("/treasury").json()
    assert t["seed_capital"] == 100.0
    assert t["gross_revenue"] == 50.0
    assert t["total_spend"] == 17.0              # 5 + 12
    assert t["net_pnl"] == 33.0                  # 50 - 17
    assert t["cash_position"] == 133.0           # seed + net_pnl


# ── END-TO-END: the demo, as a deterministic test ───────────────────────────


def _seed_cc(auto=0.50, manager=5.0, limit=50.0):
    db.init_db()
    db.upsert_cost_center("default", "Default")
    db.upsert_budget("default", limit_usd=limit, soft_threshold=0.8,
                     auto_approve_under_usd=auto, manager_under_usd=manager)


def test_demo_end_to_end(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_SEED_CAPITAL", "100.0")
    monkeypatch.setattr(enforcement, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(enforcement, "POLL_INTERVAL_SEC", 0.02)
    _seed_cc()
    c = _client()

    # 1. REVENUE — customer pays (sim path the demo driver uses).
    c.post("/revenue/sim", json={"job_id": "jDEMO", "amount_usd": 25.0, "ref": "pi_demo_rev"})

    # 2. INTENT — agent declares a $3 spend (above the $0.50 auto threshold).
    capture.request_spend("jDEMO", 3.0, session_id="sDEMO", cost_center_id="default")

    # 3. GATE — pre_tool_call blocks on a manager approval; approve it.
    box = {}

    def _gate():
        box["v"] = enforcement.on_pre_tool_call(
            "terminal", {"command": "stripe projects add openai/gpt-4o"}, "sDEMO"
        )

    th = threading.Thread(target=_gate)
    th.start()
    deadline = time.monotonic() + 3.0
    pend = None
    while time.monotonic() < deadline:
        p = db.get_pending_approvals()
        if p:
            pend = p[0]
            break
        time.sleep(0.02)
    assert pend is not None and pend["level"] == "manager"
    assert db.decide_approval(pend["id"], "approved", "human:test")
    th.join(timeout=3.0)
    assert box["v"] is None                       # approved → gate allows

    # 4. CONFIRMATION — post_tool_call records the REAL captured spend ($3.20).
    capture.on_post_tool_call(
        tool_name="terminal",
        args={"command": "stripe projects add openai/gpt-4o"},
        result=json.dumps({"output": "provisioned prod_DEMO123 charged $3.20"}),
        status="ok",
        session_id="sDEMO",
        tool_call_id="tc-demo",
    )

    # 5. ASSERT — /pnl and /treasury show the COMPUTED real numbers, not the $3 projection.
    pnl = {r["job_id"]: r for r in c.get("/pnl").json()["jobs"]}
    assert pnl["jDEMO"]["revenue"] == 25.0
    assert pnl["jDEMO"]["external_spend"] == 3.20  # confirmed, not 3.00
    assert pnl["jDEMO"]["pnl"] == 21.80

    treasury = c.get("/treasury").json()
    assert treasury["seed_capital"] == 100.0
    assert treasury["gross_revenue"] == 25.0
    assert treasury["total_spend"] == 3.20
    assert treasury["net_pnl"] == 21.80
    assert treasury["cash_position"] == 121.80    # 100 + 25 - 3.20

    # 6. Audit lifecycle, in order: revenue_received → spend_attempted → approved → confirmed.
    events = _events()
    for e in ("revenue_received", "spend_attempted", "spend_approved", "spend_confirmed"):
        assert e in events
    order = [events.index(e) for e in
             ("spend_attempted", "spend_approved", "spend_confirmed")]
    # get_recent_audit is newest-first, so later lifecycle events have SMALLER indices.
    assert order == sorted(order, reverse=True)
