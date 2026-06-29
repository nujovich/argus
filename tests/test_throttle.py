"""Mid-flight throttle (Phase 4.5c-cooperative)."""

from __future__ import annotations

import db
import hook


def _seed_allocation(*, job_id="j-1", tier="ultra", budget=10.0,
                     model="nvidia/nemotron-3-ultra-550b-a55b"):
    return db.insert_compute_allocation(
        job_id=job_id, cost_center_id="ai_research",
        tier=tier, model=model,
        compute_budget_usd=budget,
        expected_revenue_usd=120.0, expected_margin_usd=110.0,
    )


def test_burn_well_under_budget_stays_active(tmp_hermes_home):
    _seed_allocation()
    db.insert_ledger_row(job_id="j-1", kind="llm_cost", amount_usd=2.0)
    t = hook.check_and_apply_throttle("j-1")
    assert t["status"] == "active"
    assert t["burn_ratio"] == 0.2


def test_burn_breach_at_threshold_downgrades_ultra(tmp_hermes_home):
    _seed_allocation(tier="ultra", budget=10.0)
    db.insert_ledger_row(job_id="j-1", kind="llm_cost", amount_usd=8.0)
    t = hook.check_and_apply_throttle("j-1")
    assert t["status"] == "downgraded"
    assert t["burn_ratio"] == 0.8
    assert "to_model" in t
    # Audit row recorded the event.
    events = [a["event"] for a in db.get_recent_audit(10)]
    assert "compute_tier_downgraded" in events


def test_burn_breach_on_base_does_not_downgrade(tmp_hermes_home):
    """Base is already the cheap tier — nothing to downgrade to,
    just let it run. (Future enhancement: kill on margin loss.)"""
    _seed_allocation(tier="base", budget=1.0,
                     model="nvidia/nemotron-3-base-9b")
    db.insert_ledger_row(job_id="j-1", kind="llm_cost", amount_usd=0.95)
    t = hook.check_and_apply_throttle("j-1")
    assert t["status"] == "active"  # base stays active
    events = [a["event"] for a in db.get_recent_audit(10)]
    assert "compute_tier_downgraded" not in events


def test_burn_far_over_budget_kills(tmp_hermes_home):
    _seed_allocation(tier="ultra", budget=10.0)
    db.insert_ledger_row(job_id="j-1", kind="llm_cost", amount_usd=13.0)
    t = hook.check_and_apply_throttle("j-1")
    assert t["status"] == "killed"
    events = [a["event"] for a in db.get_recent_audit(10)]
    assert "compute_tier_killed" in events


def test_throttle_idempotent_after_downgrade(tmp_hermes_home):
    _seed_allocation(tier="ultra", budget=10.0)
    db.insert_ledger_row(job_id="j-1", kind="llm_cost", amount_usd=8.0)
    hook.check_and_apply_throttle("j-1")
    # Second call doesn't fire a duplicate audit row.
    audit_before = [a["event"] for a in db.get_recent_audit(20)]
    downgrades_before = audit_before.count("compute_tier_downgraded")
    hook.check_and_apply_throttle("j-1")
    audit_after = [a["event"] for a in db.get_recent_audit(20)]
    downgrades_after = audit_after.count("compute_tier_downgraded")
    assert downgrades_after == downgrades_before, "downgrade should fire only once"
