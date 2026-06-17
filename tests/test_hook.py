"""Integration tests for the pre_tool_call hook (Capture + Enforcement)."""

from __future__ import annotations

import threading
import time

import db
import hook


def test_unrelated_tool_passes_through(tmp_hermes_home):
    result = hook.on_pre_tool_call("read_file", {"path": "/tmp/x"}, "session-1")
    assert result is None
    # No audit / no rows.
    assert db.get_recent_audit(10) == []


def test_missing_declaration_is_logged_and_allowed(tmp_hermes_home):
    result = hook.on_pre_tool_call("stripe_create_payment_intent", {}, "session-1")
    assert result is None
    events = [a["event"] for a in db.get_recent_audit(10)]
    assert "spend_skipped_missing_declaration" in events


def test_auto_approve_under_threshold(tmp_hermes_home):
    args = {"job_id": "j1", "cost_center_id": "default", "projected_usd": 0.25}
    result = hook.on_pre_tool_call("argus_request_spend", args, "session-1")
    assert result is None  # allowed
    # Declaration auto-approved → external_spend row recorded.
    rows = {r["job_id"]: r for r in db.get_pnl_per_job()}
    assert rows["j1"]["external_spend"] == 0.25
    assert any(
        a["event"] == "spend_evaluated" and a["payload"]["verdict"] == "ALLOW"
        for a in db.get_recent_audit(10)
    )


def test_manager_tier_holds_until_approval(tmp_hermes_home, monkeypatch):
    # Speed the polling loop way up so the test runs in well under a second.
    monkeypatch.setattr(hook, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(hook, "POLL_INTERVAL_SEC", 0.05)

    args = {"job_id": "j1", "cost_center_id": "default", "projected_usd": 5.0}
    result_box: dict = {}

    def runner():
        result_box["v"] = hook.on_pre_tool_call("argus_request_spend", args, "s1")

    t = threading.Thread(target=runner)
    t.start()

    # Give the hook a moment to enqueue the approval, then approve it.
    deadline = time.monotonic() + 2.0
    pending: list = []
    while time.monotonic() < deadline:
        pending = db.get_pending_approvals()
        if pending:
            break
        time.sleep(0.02)
    assert pending, "approval request was never enqueued"
    assert pending[0]["level"] == "manager"

    ok = db.decide_approval(pending[0]["id"], decision="approved", actor="human:test")
    assert ok

    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result_box["v"] is None  # approved → tool proceeds

    # Spend row was written on the approve branch.
    rows = {r["job_id"]: r for r in db.get_pnl_per_job()}
    assert rows["j1"]["external_spend"] == 5.0


def test_rejection_blocks_with_message(tmp_hermes_home, monkeypatch):
    monkeypatch.setattr(hook, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(hook, "POLL_INTERVAL_SEC", 0.05)

    args = {"job_id": "j1", "cost_center_id": "default", "projected_usd": 5.0}
    result_box: dict = {}

    def runner():
        result_box["v"] = hook.on_pre_tool_call("argus_request_spend", args, "s1")

    t = threading.Thread(target=runner)
    t.start()

    deadline = time.monotonic() + 2.0
    pending: list = []
    while time.monotonic() < deadline:
        pending = db.get_pending_approvals()
        if pending:
            break
        time.sleep(0.02)
    assert pending
    db.decide_approval(pending[0]["id"], decision="rejected", actor="human:test", reason="nope")

    t.join(timeout=3.0)
    assert not t.is_alive()
    v = result_box["v"]
    assert isinstance(v, dict) and v.get("action") == "block"
    assert "Argus blocked" in v["message"]

    # No spend row should have been written on the reject branch.
    rows = db.get_pnl_per_job()
    assert all(r["external_spend"] == 0 for r in rows)


def test_timeout_blocks(tmp_hermes_home, monkeypatch):
    monkeypatch.setattr(hook, "APPROVAL_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(hook, "POLL_INTERVAL_SEC", 0.05)

    args = {"job_id": "j1", "cost_center_id": "default", "projected_usd": 5.0}
    result = hook.on_pre_tool_call("argus_request_spend", args, "s1")
    assert isinstance(result, dict) and result.get("action") == "block"
    # The approval row should be marked timeout.
    rows = db.get_recent_approvals(10)
    assert rows and rows[0]["status"] == "timeout"
