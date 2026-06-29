"""Compute Allocator policy — pure-function tests (Phase 4.5)."""

from __future__ import annotations

import policy


def _snap(*, min_rev=100.0, min_mgn=50.0, reject_below=0.0,
          spent=0.0, limit=float("inf")):
    return policy.ComputeSnapshot(
        cost_center_id="ai_research",
        ultra_model="nvidia/nemotron-3-ultra-550b-a55b",
        base_model="nvidia/nemotron-3-base-9b",
        ultra_min_revenue_usd=min_rev,
        ultra_min_margin_usd=min_mgn,
        reject_below_margin_usd=reject_below,
        monthly_spent_usd=spent,
        monthly_limit_usd=limit,
    )


def _decl(*, revenue, burn, job="j-1"):
    return policy.ComputeDeclaration(
        job_id=job,
        cost_center_id="ai_research",
        expected_revenue_usd=revenue,
        projected_burn_usd=burn,
    )


def test_premium_job_gets_ultra():
    # $200 revenue, $15 burn → $185 margin → meets both thresholds → Ultra.
    d = policy.decide_compute_tier(_decl(revenue=200, burn=15), _snap())
    assert d.verdict == "TIER_ULTRA"
    assert d.tier_label == "ultra"
    assert d.model == "nvidia/nemotron-3-ultra-550b-a55b"
    assert d.compute_budget_usd == 15.0
    assert d.expected_margin_usd == 185.0


def test_low_revenue_routes_to_base_even_with_good_margin():
    # $50 revenue is below ultra_min_revenue $100, so even with $48 margin
    # the job runs on Base.
    d = policy.decide_compute_tier(_decl(revenue=50, burn=2), _snap(min_rev=100))
    assert d.verdict == "TIER_BASE"
    assert d.model == "nvidia/nemotron-3-base-9b"


def test_thin_margin_routes_to_base_even_with_high_revenue():
    # $500 revenue but only $20 margin (under $50 ultra_min_margin)
    # → Base. Margin matters, not just headline number.
    d = policy.decide_compute_tier(_decl(revenue=500, burn=480), _snap())
    assert d.verdict == "TIER_BASE"


def test_negative_margin_rejected():
    # $3 revenue, $5 burn → -$2 margin → reject.
    d = policy.decide_compute_tier(_decl(revenue=3, burn=5), _snap(reject_below=0))
    assert d.verdict == "TIER_REJECT"
    assert "negative_margin" in d.reason
    assert d.is_rejected


def test_monthly_cap_breach_escalates_to_manager():
    # $50 already spent, $40 more requested, but cap is $80.
    d = policy.decide_compute_tier(
        _decl(revenue=200, burn=40),
        _snap(spent=50, limit=80),
    )
    assert d.verdict == "NEEDS_APPROVAL_MANAGER"
    assert "monthly_cap_breach" in d.reason


def test_boundary_at_ultra_revenue_threshold():
    # Exactly at threshold → Ultra (gte, not gt).
    d = policy.decide_compute_tier(_decl(revenue=100, burn=10), _snap(min_rev=100, min_mgn=50))
    assert d.verdict == "TIER_ULTRA"


def test_compute_decision_is_frozen():
    d = policy.decide_compute_tier(_decl(revenue=200, burn=15), _snap())
    try:
        d.verdict = "TIER_BASE"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ComputeDecision should be frozen")
