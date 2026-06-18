"""Tests for the Enforcement layer (enforcement.on_pre_tool_call).

Unit (matcher / resolution / snapshot) + real-store integration (verdict→return
with a thread flipping the approval row) + the all-important FAIL-CLOSED matrix.
No live Hermes needed — the live hold is proven by the §6 spike.
"""

from __future__ import annotations

import threading
import time

import pytest

import db
import enforcement
import policy


@pytest.fixture(autouse=True)
def _clear_decls():
    enforcement.clear_declarations()
    yield
    enforcement.clear_declarations()


def _term(command):
    return {"command": command}


# ── matcher precision ────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", [
    "stripe projects add openai/gpt-4o",
    "stripe projects upgrade openai",
    "stripe-link-cli mpp pay --amount 12.50",
    "npx @stripe/link-cli foo mpp pay $7.00",
])
def test_spend_commands_match(command):
    assert enforcement.is_spend_command(command)
    assert enforcement._command_of("terminal", _term(command)) == command


@pytest.mark.parametrize("command", [
    "stripe projects list",
    "stripe projects catalog",
    "stripe projects status",
    "stripe projects init",
    "stripe auth status",
    "ls -la",
    "echo hello world",
    "git commit -m 'mpp'",
])
def test_non_spend_commands_do_not_match(command):
    assert not enforcement.is_spend_command(command)


def test_non_terminal_tool_never_matched():
    # Even a stripe-ish payload under a non-terminal tool is ignored here.
    assert enforcement._command_of("stripe_create", {"command": "stripe projects add x"}) is None
    assert enforcement.on_pre_tool_call("read_file", {"path": "/tmp/x"}, "s1") is None


# ── projected_usd resolution ─────────────────────────────────────────────────


def test_resolution_prefers_declaration():
    enforcement.declare_spend("jD", 42.0, cost_center_id="cc", session_id="sess1")
    decl = enforcement._lookup_declaration("sess1")
    amt, src = enforcement._resolve_projected(decl, "stripe projects add x")
    assert amt == 42.0 and src == "declaration"


def test_resolution_parses_mpp_amount():
    amt, src = enforcement._resolve_projected(None, "link-cli mpp pay --amount 12.50")
    assert amt == 12.50 and src == "parsed"
    amt2, _ = enforcement._resolve_projected(None, "mpp pay $7")
    assert amt2 == 7.0


def test_resolution_unknown_forces_finance_never_auto(tmp_hermes_home):
    # No declaration, no parseable amount → undeclared → finance approval,
    # never auto-approved even though auto threshold would pass a small amount.
    db.init_db()
    enforcement.APPROVAL_TIMEOUT_SEC_orig = enforcement.APPROVAL_TIMEOUT_SEC

    captured = {}

    real_create = db.create_approval

    def spy_create(job_id, cost_center_id, projected_usd, level="unspecified"):
        captured["level"] = level
        captured["projected"] = projected_usd
        return real_create(job_id, cost_center_id, projected_usd, level=level)

    db.create_approval = spy_create
    try:
        # tiny timeout so the hold returns quickly as a block
        enforcement.APPROVAL_TIMEOUT_SEC = 0.2
        enforcement.POLL_INTERVAL_SEC = 0.05
        result = enforcement.on_pre_tool_call(
            "terminal", _term("stripe projects add foo"), "sessU"
        )
    finally:
        db.create_approval = real_create
        enforcement.APPROVAL_TIMEOUT_SEC = enforcement.APPROVAL_TIMEOUT_SEC_orig
        enforcement.POLL_INTERVAL_SEC = 0.5

    assert captured["level"] == "finance"  # forced finance, not auto/manager
    assert isinstance(result, dict) and result["action"] == "block"  # timed out → block
    events = [a["event"] for a in db.get_recent_audit(20)]
    assert "spend_attempted" in events and "approval_requested" in events


# ── snapshot composition ─────────────────────────────────────────────────────


def test_snapshot_composition_matches_policy_shape(tmp_hermes_home):
    db.init_db()
    db.upsert_cost_center("ccS", "S")
    db.upsert_budget("ccS", limit_usd=100.0, soft_threshold=0.8,
                     auto_approve_under_usd=1.0, manager_under_usd=10.0)
    db.register_job("jS", "ccS")
    db.append_fact("jS", "revenue", 200.0)
    db.append_fact("jS", "external_spend", 15.0)
    db.append_fact("jS", "llm_cost", 5.0)

    snap = enforcement._compose_snapshot("ccS", "jS")
    assert isinstance(snap, policy.SpendSnapshot)
    assert isinstance(snap.budget, policy.BudgetLimits)
    assert snap.budget.limit_usd == 100.0
    assert snap.budget.auto_approve_under_usd == 1.0
    assert snap.budget.manager_under_usd == 10.0
    assert snap.cost_center_used_usd == 20.0      # 15 + 5 (revenue excluded)
    assert snap.job_revenue_usd == 200.0
    assert snap.job_spend_so_far_usd == 20.0


# ── verdict → return mapping with a REAL store + a deciding thread ───────────


def _seed_cc(limit=25.0, auto=0.50, manager=5.0):
    db.init_db()
    db.upsert_cost_center("default", "Default")
    db.upsert_budget("default", limit_usd=limit, soft_threshold=0.8,
                     auto_approve_under_usd=auto, manager_under_usd=manager)


def test_allow_auto_returns_none_and_runs(tmp_hermes_home):
    _seed_cc(auto=0.50)
    enforcement.declare_spend("jA", 0.25, cost_center_id="default", session_id="sA")
    result = enforcement.on_pre_tool_call("terminal", _term("stripe projects add x"), "sA")
    assert result is None  # ALLOW → Hermes runs the command
    assert any(a["event"] == "spend_approved" and a["payload"].get("mode") == "auto"
               for a in db.get_recent_audit(20))


def _run_hook_in_thread(command, session_id):
    box = {}

    def runner():
        box["v"] = enforcement.on_pre_tool_call("terminal", _term(command), session_id)

    t = threading.Thread(target=runner)
    t.start()
    return t, box


def _await_pending():
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pend = db.get_pending_approvals()
        if pend:
            return pend[0]
        time.sleep(0.02)
    raise AssertionError("approval never enqueued")


def test_approve_resumes_to_allow(tmp_hermes_home, monkeypatch):
    _seed_cc(manager=5.0)
    monkeypatch.setattr(enforcement, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(enforcement, "POLL_INTERVAL_SEC", 0.05)
    enforcement.declare_spend("jM", 3.0, cost_center_id="default", session_id="sM")

    t, box = _run_hook_in_thread("stripe projects add x", "sM")
    pend = _await_pending()
    assert pend["level"] == "manager"
    assert db.decide_approval(pend["id"], "approved", "human:test")
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert box["v"] is None  # approved → resume (allow)


def test_reject_returns_block(tmp_hermes_home, monkeypatch):
    _seed_cc(manager=5.0)
    monkeypatch.setattr(enforcement, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(enforcement, "POLL_INTERVAL_SEC", 0.05)
    enforcement.declare_spend("jR", 3.0, cost_center_id="default", session_id="sR")

    t, box = _run_hook_in_thread("stripe projects add x", "sR")
    pend = _await_pending()
    assert db.decide_approval(pend["id"], "rejected", "human:test", "not now")
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert isinstance(box["v"], dict) and box["v"]["action"] == "block"
    assert "rejected" in box["v"]["message"]


def test_timeout_returns_block_and_marks_row(tmp_hermes_home, monkeypatch):
    _seed_cc(manager=5.0)
    monkeypatch.setattr(enforcement, "APPROVAL_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(enforcement, "POLL_INTERVAL_SEC", 0.05)
    enforcement.declare_spend("jT", 3.0, cost_center_id="default", session_id="sT")

    result = enforcement.on_pre_tool_call("terminal", _term("stripe projects add x"), "sT")
    assert isinstance(result, dict) and result["action"] == "block"
    assert "timeout" in result["message"]
    row = db.get_recent_approvals(5)[0]
    assert row["status"] == "timeout"  # a REAL decide on the row


def test_poll_path_has_no_locked_errors(tmp_hermes_home, monkeypatch):
    """The hold poll runs while another thread writes — must use the WAL-robust
    read path and never surface a locked error (it would mean a block leak)."""
    _seed_cc(manager=5.0)
    monkeypatch.setattr(enforcement, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(enforcement, "POLL_INTERVAL_SEC", 0.02)
    enforcement.declare_spend("jP", 3.0, cost_center_id="default", session_id="sP")

    t, box = _run_hook_in_thread("stripe projects add x", "sP")
    pend = _await_pending()
    # Hammer writes while the hook polls, then approve.
    for i in range(20):
        db.append_audit("noise", "tick", {"i": i})
    db.decide_approval(pend["id"], "approved", "human:test")
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert box["v"] is None  # resumed cleanly, no exception/block


# ── FAIL-CLOSED — the most important test: no path leaks an allow ────────────


def test_fail_closed_on_snapshot_error(tmp_hermes_home, monkeypatch):
    _seed_cc()
    enforcement.declare_spend("jE", 3.0, cost_center_id="default", session_id="sE")

    def boom(*a, **k):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(enforcement, "_compose_snapshot", boom)
    result = enforcement.on_pre_tool_call("terminal", _term("stripe projects add x"), "sE")
    assert isinstance(result, dict) and result["action"] == "block"


def test_fail_closed_on_policy_error(tmp_hermes_home, monkeypatch):
    _seed_cc()
    enforcement.declare_spend("jE", 3.0, cost_center_id="default", session_id="sE2")

    def boom(*a, **k):
        raise ValueError("policy exploded")

    monkeypatch.setattr(policy, "evaluate_spend", boom)
    result = enforcement.on_pre_tool_call("terminal", _term("stripe projects add x"), "sE2")
    assert isinstance(result, dict) and result["action"] == "block"


def test_fail_closed_on_db_error(tmp_hermes_home, monkeypatch):
    _seed_cc()
    enforcement.declare_spend("jE", 3.0, cost_center_id="default", session_id="sE3")

    def boom(*a, **k):
        raise RuntimeError("db create_approval exploded")

    monkeypatch.setattr(db, "create_approval", boom)
    result = enforcement.on_pre_tool_call("terminal", _term("stripe projects add x"), "sE3")
    assert isinstance(result, dict) and result["action"] == "block"


def test_fail_closed_even_when_audit_also_fails(tmp_hermes_home, monkeypatch):
    # Worst case: the gate raises AND the error-audit write also raises.
    _seed_cc()
    enforcement.declare_spend("jE", 3.0, cost_center_id="default", session_id="sE4")
    monkeypatch.setattr(enforcement, "_compose_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(db, "append_audit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down")))
    result = enforcement.on_pre_tool_call("terminal", _term("stripe projects add x"), "sE4")
    assert isinstance(result, dict) and result["action"] == "block"


# ── audit lifecycle ──────────────────────────────────────────────────────────


def test_audit_lifecycle_full(tmp_hermes_home, monkeypatch):
    _seed_cc(manager=5.0)
    monkeypatch.setattr(enforcement, "APPROVAL_TIMEOUT_SEC", 5)
    monkeypatch.setattr(enforcement, "POLL_INTERVAL_SEC", 0.05)
    enforcement.declare_spend("jL", 3.0, cost_center_id="default", session_id="sL")

    t, box = _run_hook_in_thread("stripe projects add x", "sL")
    pend = _await_pending()
    db.decide_approval(pend["id"], "approved", "human:boss")
    t.join(timeout=3.0)

    events = [a["event"] for a in db.get_recent_audit(50)]
    assert "spend_attempted" in events
    assert "approval_requested" in events
    assert "spend_approved" in events
