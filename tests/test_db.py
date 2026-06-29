"""Smoke tests for the ledger writer/reader path."""

from __future__ import annotations

import db


def test_schema_creates_tables(tmp_hermes_home):
    conn = db._get_conn()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"ledger", "approval_requests", "audit_trail"} <= tables


def test_pnl_roundtrip(tmp_hermes_home):
    db.insert_ledger_row(job_id="j1", kind="revenue", amount_usd=100.0)
    db.insert_ledger_row(job_id="j1", kind="external_spend", amount_usd=30.0)
    db.insert_ledger_row(job_id="j1", kind="llm_cost", amount_usd=5.0)
    db.insert_ledger_row(job_id="j2", kind="revenue", amount_usd=10.0)

    rows = {r["job_id"]: r for r in db.get_pnl_per_job()}
    assert rows["j1"]["revenue"] == 100.0
    assert rows["j1"]["external_spend"] == 30.0
    assert rows["j1"]["llm_cost"] == 5.0
    assert rows["j1"]["pnl"] == 65.0
    assert rows["j2"]["pnl"] == 10.0


def test_approval_lifecycle(tmp_hermes_home):
    req_id = db.create_approval_request(
        job_id="j1", cost_center_id="default", projected_usd=7.5, level="manager"
    )
    assert db.get_approval_status(req_id) == "pending"
    assert len(db.get_pending_approvals()) == 1

    ok = db.decide_approval(req_id, decision="approved", actor="human:test")
    assert ok
    assert db.get_approval_status(req_id) == "approved"
    assert db.get_pending_approvals() == []

    # Second decide is a no-op (already decided).
    ok2 = db.decide_approval(req_id, decision="rejected", actor="human:test")
    assert ok2 is False


def test_cost_center_spent_sums_only_spend_kinds(tmp_hermes_home):
    db.insert_ledger_row(job_id="j1", kind="revenue", amount_usd=100.0)
    db.insert_ledger_row(job_id="j1", kind="llm_cost", amount_usd=2.0)
    db.insert_ledger_row(job_id="j1", kind="external_spend", amount_usd=3.0)
    db.insert_ledger_row(job_id="j2", kind="external_spend", amount_usd=99.0)

    assert db.get_cost_center_spent("default", ["j1"]) == 5.0
    # j2's spend is not attributed to this caller's job list.
    assert db.get_cost_center_spent("default", ["j1", "j2"]) == 104.0


def test_audit_trail(tmp_hermes_home):
    db.log_audit("system", "spend_evaluated", {"verdict": "ALLOW"})
    db.log_audit("human:alice", "approval_approved", {"approval_id": "x"})
    items = db.get_recent_audit(10)
    assert [i["event"] for i in items] == ["approval_approved", "spend_evaluated"]
    assert items[0]["payload"] == {"approval_id": "x"}
