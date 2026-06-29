"""Compute integrity — silent-fallback detection (Phase 4.5b)."""

from __future__ import annotations

import db


def test_no_allocations_no_violations(tmp_hermes_home):
    violations = db.run_compute_integrity_sweep()
    assert violations == []


def test_authorized_model_matches_actual_no_violation(tmp_hermes_home):
    aid = db.insert_compute_allocation(
        job_id="j1", cost_center_id="ai_research",
        tier="ultra", model="nvidia/nemotron-3-ultra-550b-a55b",
        compute_budget_usd=15.0,
        expected_revenue_usd=200.0, expected_margin_usd=185.0,
        session_id="sess-1",
    )
    # Authorized = observed: no violation
    db.set_actual_model(aid, "nvidia/nemotron-3-ultra-550b-a55b")
    violations = db.run_compute_integrity_sweep()
    assert violations == []


def test_silent_fallback_is_detected(tmp_hermes_home):
    aid = db.insert_compute_allocation(
        job_id="j-premium", cost_center_id="ai_research",
        tier="ultra", model="nvidia/nemotron-3-ultra-550b-a55b",
        compute_budget_usd=15.0,
        expected_revenue_usd=200.0, expected_margin_usd=185.0,
        session_id="sess-fallback",
    )
    # Agent thought it was on Ultra; actually ran on Base.
    db.set_actual_model(aid, "nvidia/nemotron-3-base-9b")
    violations = db.run_compute_integrity_sweep()
    assert len(violations) == 1
    v = violations[0]
    assert v["job_id"] == "j-premium"
    assert v["authorized_model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert v["observed_model"] == "nvidia/nemotron-3-base-9b"
    assert v["tier_authorized"] == "ultra"

    # The audit trail records the violation.
    events = [a["event"] for a in db.get_recent_audit(10)]
    assert "compute_integrity_violation" in events
    assert "compute_integrity_sweep" in events


def test_rejected_allocations_skip_integrity(tmp_hermes_home):
    """Reject allocations never ran inference — integrity sweep ignores them."""
    aid = db.insert_compute_allocation(
        job_id="j-rejected", cost_center_id="ai_research",
        tier="reject", model="",
        compute_budget_usd=0.0,
        expected_revenue_usd=2.0, expected_margin_usd=-18.0,
    )
    # Even if someone set actual_model on a rejected alloc, no violation.
    db.set_actual_model(aid, "nvidia/nemotron-3-ultra-550b-a55b")
    violations = db.run_compute_integrity_sweep()
    assert violations == []


def test_sweep_marks_integrity_status(tmp_hermes_home):
    """Sweep updates the integrity_status column so the fleet view can
    show a 🚨 badge."""
    a_ok = db.insert_compute_allocation(
        job_id="j-ok", cost_center_id="ai_research",
        tier="ultra", model="nvidia/nemotron-3-ultra-550b-a55b",
        compute_budget_usd=15.0, expected_revenue_usd=200.0,
        expected_margin_usd=185.0,
    )
    a_violation = db.insert_compute_allocation(
        job_id="j-violation", cost_center_id="ai_research",
        tier="ultra", model="nvidia/nemotron-3-ultra-550b-a55b",
        compute_budget_usd=15.0, expected_revenue_usd=200.0,
        expected_margin_usd=185.0,
    )
    db.set_actual_model(a_ok, "nvidia/nemotron-3-ultra-550b-a55b")
    db.set_actual_model(a_violation, "nvidia/nemotron-3-base-9b")

    db.run_compute_integrity_sweep()
    allocs = {a["job_id"]: a for a in db.get_compute_allocations()}
    assert allocs["j-ok"]["integrity_status"] == "ok"
    assert allocs["j-violation"]["integrity_status"] == "violation"
