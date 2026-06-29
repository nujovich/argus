"""Tests for the pure spend policy (policy.evaluate_spend).

Policy is the brain: a pure function (job, projected, snapshot) -> verdict.
These tests need NO db, no fixtures, no clock — that is the whole point.
See CLAUDE.md §2 / §3 / §8 / §11.
"""

from __future__ import annotations

import ast
from pathlib import Path

import policy
from policy import BudgetLimits, SpendSnapshot, evaluate_spend


def _snap(
    *,
    used=0.0,
    limit=25.0,
    soft=0.8,
    auto=0.50,
    manager=5.0,
    revenue=None,
    spend_so_far=None,
):
    return SpendSnapshot(
        cost_center_id="cc",
        budget=BudgetLimits(
            limit_usd=limit,
            soft_threshold=soft,
            auto_approve_under_usd=auto,
            manager_under_usd=manager,
        ),
        cost_center_used_usd=used,
        job_revenue_usd=revenue,
        job_spend_so_far_usd=spend_so_far,
    )


def _ev(projected, snap):
    return evaluate_spend("job1", "cc", projected, snap)


# ── the routing matrix ──────────────────────────────────────────────────────


def test_under_auto_threshold_allows():
    v = _ev(0.25, _snap(auto=0.50))
    assert v.allowed and v.decision == "ALLOW"
    assert v.tier is None
    assert not v.breach


def test_medium_within_budget_set_manager_cap_routes_manager():
    v = _ev(3.0, _snap(used=0.0, limit=25.0, manager=5.0))
    assert v.needs_approval and v.tier == "manager"
    assert not v.breach


def test_large_within_budget_routes_finance():
    # projected >= manager_under_usd but still within the limit -> finance.
    v = _ev(8.0, _snap(used=0.0, limit=25.0, manager=5.0))
    assert v.needs_approval and v.tier == "finance"
    assert not v.breach


def test_breach_escalates_to_finance_even_at_manager_amount():
    # Amount alone (3.0 < manager 5.0) would be manager-tier, but the
    # cumulative center total breaches the limit -> finance. This is the
    # cumulative-ledger differentiator vs a static per-call cap.
    v = _ev(3.0, _snap(used=24.0, limit=25.0, manager=5.0))
    assert v.tier == "finance"
    assert v.breach is True
    assert v.projected_center_total_usd == 27.0


def test_soft_threshold_crossed_but_not_breached_routes_by_amount():
    # used 19 + projected 3 = 22 > soft(0.8*25=20) but <= limit 25.
    v = _ev(3.0, _snap(used=19.0, limit=25.0, soft=0.8, manager=5.0))
    assert v.soft is True
    assert v.breach is False
    assert v.tier == "manager"  # routed by amount since no breach


def test_manager_cap_null_sends_anything_above_auto_to_finance():
    v = _ev(1.0, _snap(auto=0.50, manager=None))
    assert v.tier == "finance"
    assert "no_manager_tier" in v.reason


def test_null_manager_still_auto_approves_small():
    v = _ev(0.40, _snap(auto=0.50, manager=None))
    assert v.allowed


# ── margin-awareness (the demo's key beat) ──────────────────────────────────


def test_profitable_but_over_budget_finance_with_positive_margin_surfaced():
    # Job earns $200, has spent $5 so far, wants $30 more; center is near cap so
    # this breaches -> finance, yet the margin is strongly positive and surfaced.
    v = _ev(
        30.0,
        _snap(used=200.0, limit=200.0, manager=50.0, revenue=200.0, spend_so_far=5.0),
    )
    assert v.tier == "finance"
    assert v.breach is True
    assert v.job_margin_if_approved_usd == 165.0  # 200 - 5 - 30
    assert "margin_positive" in v.reason


def test_margin_unknown_when_no_revenue_info():
    v = _ev(3.0, _snap(manager=5.0))
    assert v.job_margin_if_approved_usd is None
    assert "margin_unknown" in v.reason


def test_negative_margin_is_surfaced_not_gated():
    # Margin negative but Policy never auto-rejects — humans decide.
    v = _ev(8.0, _snap(used=0.0, limit=25.0, manager=5.0, revenue=3.0, spend_so_far=0.0))
    assert v.needs_approval  # not REJECT — there is no reject verdict
    assert v.job_margin_if_approved_usd == -5.0  # 3 - 0 - 8
    assert "margin_negative" in v.reason


def test_money_quantized_to_cents():
    v = _ev(0.005, _snap(auto=0.0, used=0.004, limit=25.0, manager=5.0))
    # projected 0.005 -> 0.01 (rounded); center total 0.004 + 0.01 -> 0.01.
    assert v.projected_usd == 0.01
    assert v.projected_center_total_usd == 0.01


# ── purity ──────────────────────────────────────────────────────────────────


def test_purity_identical_inputs_identical_verdict():
    snap = _snap(used=24.0, limit=25.0, manager=5.0, revenue=200.0, spend_so_far=5.0)
    v1 = _ev(3.0, snap)
    v2 = _ev(3.0, snap)
    assert v1 == v2  # frozen dataclass equality — deterministic


def test_policy_module_imports_no_io():
    """Static guard (§11): the policy module must not import db, a clock,
    randomness, Stripe, or any I/O library."""
    src = Path(policy.__file__).read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    forbidden = {
        "db", "sqlite3", "time", "datetime", "random", "stripe",
        "requests", "httpx", "os", "config",
    }
    leaked = imported & forbidden
    assert not leaked, f"policy must stay pure; forbidden imports present: {leaked}"
