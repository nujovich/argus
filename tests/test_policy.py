"""Policy is pure — these tests run without touching the DB."""

from __future__ import annotations

import policy


def _snap(*, limit=50.0, spent=0.0, auto=1.0, mgr=10.0):
    return policy.BudgetSnapshot(
        cost_center_id="default",
        limit_usd=limit,
        spent_usd=spent,
        auto_approve_under_usd=auto,
        manager_under_usd=mgr,
    )


def _decl(amount):
    return policy.SpendDeclaration(
        job_id="j1", cost_center_id="default", projected_usd=amount
    )


def test_non_positive_projection_is_allowed():
    d = policy.decide(_decl(0), _snap())
    assert d.verdict == "ALLOW"
    d = policy.decide(_decl(-5), _snap())
    assert d.verdict == "ALLOW"


def test_under_auto_threshold_is_allowed():
    d = policy.decide(_decl(0.5), _snap(auto=1.0))
    assert d.verdict == "ALLOW"
    assert d.reason == "under_auto_threshold"


def test_above_auto_under_manager_routes_to_manager():
    d = policy.decide(_decl(5.0), _snap(auto=1.0, mgr=10.0))
    assert d.verdict == "NEEDS_APPROVAL_MANAGER"
    assert d.level == "manager"


def test_above_manager_routes_to_finance():
    d = policy.decide(_decl(20.0), _snap(limit=50.0, auto=1.0, mgr=10.0))
    assert d.verdict == "NEEDS_APPROVAL_FINANCE"
    assert d.level == "finance"


def test_hard_cap_breach_always_finance_even_if_under_auto():
    # projected is small, but spent already exhausts the budget.
    d = policy.decide(_decl(0.5), _snap(limit=50.0, spent=49.9, auto=1.0))
    assert d.verdict == "NEEDS_APPROVAL_FINANCE"
    assert "hard_cap_breach" in d.reason


def test_manager_tier_disabled_falls_through_to_finance():
    d = policy.decide(_decl(5.0), _snap(auto=1.0, mgr=None))
    assert d.verdict == "NEEDS_APPROVAL_FINANCE"


def test_boundary_at_auto_threshold_is_allowed():
    d = policy.decide(_decl(1.0), _snap(auto=1.0))
    assert d.verdict == "ALLOW"


def test_boundary_at_manager_threshold_is_manager():
    d = policy.decide(_decl(10.0), _snap(auto=1.0, mgr=10.0))
    assert d.verdict == "NEEDS_APPROVAL_MANAGER"


def test_decision_is_immutable():
    d = policy.decide(_decl(5.0), _snap())
    # dataclass(frozen=True): mutation raises
    try:
        d.verdict = "ALLOW"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Decision should be frozen")
