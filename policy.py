"""Argus policy — the brain.

Pure function: input is (declaration, snapshot), output is a verdict. No I/O,
no clock, no randomness. Snapshot is built by the caller from db.py readers.
See CLAUDE.md §2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class SpendDeclaration:
    job_id: str
    cost_center_id: str
    projected_usd: float
    tool_name: Optional[str] = None
    ref: Optional[str] = None


@dataclass(frozen=True)
class BudgetSnapshot:
    cost_center_id: str
    limit_usd: float
    spent_usd: float
    auto_approve_under_usd: float
    manager_under_usd: Optional[float]


Verdict = Literal["ALLOW", "NEEDS_APPROVAL_MANAGER", "NEEDS_APPROVAL_FINANCE"]


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str

    @property
    def needs_approval(self) -> bool:
        return self.verdict != "ALLOW"

    @property
    def level(self) -> Optional[str]:
        if self.verdict == "NEEDS_APPROVAL_MANAGER":
            return "manager"
        if self.verdict == "NEEDS_APPROVAL_FINANCE":
            return "finance"
        return None


def decide(decl: SpendDeclaration, snap: BudgetSnapshot) -> Decision:
    """Return the verdict for a projected spend.

    Rules (in order):
      1. Negative or zero projected spend → ALLOW (nothing to gate).
      2. spent + projected > limit → finance (hard cap breach).
      3. projected ≤ auto_approve_under_usd → ALLOW.
      4. projected ≤ manager_under_usd (if configured) → manager.
      5. otherwise → finance.
    """
    if decl.projected_usd <= 0:
        return Decision("ALLOW", "non_positive_projection")

    if snap.spent_usd + decl.projected_usd > snap.limit_usd:
        return Decision(
            "NEEDS_APPROVAL_FINANCE",
            f"hard_cap_breach: spent {snap.spent_usd:.2f} + projected"
            f" {decl.projected_usd:.2f} > limit {snap.limit_usd:.2f}",
        )

    if decl.projected_usd <= snap.auto_approve_under_usd:
        return Decision("ALLOW", "under_auto_threshold")

    if snap.manager_under_usd is not None and decl.projected_usd <= snap.manager_under_usd:
        return Decision("NEEDS_APPROVAL_MANAGER", "manager_tier")

    return Decision("NEEDS_APPROVAL_FINANCE", "finance_tier")


# ---------------------------------------------------------------------------
# Spend policy v2 — richer, dashboard-ready verdict (CLAUDE.md §2, §3, §8)
#
# Same pure-function contract as decide() above, but surfaces the data the
# approval card needs: projected cost-center total, the limit, breach/soft
# flags, and the job margin-if-approved (the "profitable but over budget" beat).
# Additive: decide() and its callers (hook.py) are intentionally left untouched.
#
# NOTE — behavioural difference vs decide(): per CLAUDE.md §3, auto-approve is
# evaluated FIRST here, so a sub-threshold spend that nonetheless breaches the
# cost-center limit is ALLOWed (auto wins). decide() escalates breaches first.
# evaluate_spend follows the §3 routing precisely; pick one surface per caller.
#
# NOTE — snapshot shape gap: the Ledger store today exposes ledger_snapshot()
# -> {cost_center_id, spent_usd, limit_usd, remaining_usd, soft_threshold} and
# budget_for() -> the budgets row. Neither returns auto_approve_under_usd +
# manager_under_usd + per-job revenue/spend together in the shape below. The
# CALLER (Enforcement) composes SpendSnapshot from budget_for() + ledger_snapshot()
# + per-job revenue/spend. Policy only consumes it. Do NOT change the store here.
# ---------------------------------------------------------------------------

_CENTS = 2


def _q(amount: float) -> float:
    """Quantize to cents, matching the Ledger's money convention. Pure."""
    return round(float(amount), _CENTS)


SpendTier = Literal["manager", "finance"]


@dataclass(frozen=True)
class BudgetLimits:
    """The §8 budgets columns Policy needs. ``manager_under_usd`` may be None."""
    limit_usd: float
    soft_threshold: float
    auto_approve_under_usd: float
    manager_under_usd: Optional[float] = None


@dataclass(frozen=True)
class SpendSnapshot:
    """Everything Policy needs to decide, supplied by the caller (no I/O here).

    ``job_revenue_usd`` / ``job_spend_so_far_usd`` may be 0 or None when revenue
    hasn't been declared yet — margin is then surfaced as None (unknown), never
    used to gate.
    """
    cost_center_id: str
    budget: BudgetLimits
    cost_center_used_usd: float
    job_revenue_usd: Optional[float] = None
    job_spend_so_far_usd: Optional[float] = None


@dataclass(frozen=True)
class SpendVerdict:
    """Pure result of evaluate_spend. ``decision`` is ALLOW or NEEDS_APPROVAL;
    ``tier`` is set only when approval is needed. The remaining fields are
    COMPUTED from inputs for the approval card — Policy never fetches them."""
    decision: Literal["ALLOW", "NEEDS_APPROVAL"]
    reason: str
    projected_usd: float
    projected_center_total_usd: float
    limit_usd: float
    breach: bool
    soft: bool
    tier: Optional[SpendTier] = None
    job_margin_if_approved_usd: Optional[float] = None

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    @property
    def needs_approval(self) -> bool:
        return self.decision == "NEEDS_APPROVAL"


def evaluate_spend(
    job_id: str,
    cost_center_id: str,
    projected_usd: float,
    snapshot: SpendSnapshot,
) -> SpendVerdict:
    """Pure: (job, projected_spend, snapshot) -> verdict. No I/O, no clock,
    no randomness. Same inputs always yield the same verdict.

    Routing (CLAUDE.md §3 + §8 budget columns):
      1. projected <= auto_approve_under_usd            -> ALLOW (auto)
      2. else tier by amount AND budget state:
           projected < manager_under_usd AND not breach -> manager
           otherwise (large | manager NULL | breach)    -> finance
         A budget breach always escalates to finance.
      3. margin is informational only (surfaced, never a gate).
    """
    b = snapshot.budget
    projected = _q(projected_usd)
    limit = _q(b.limit_usd)

    projected_center_total = _q(snapshot.cost_center_used_usd + projected)
    breach = projected_center_total > limit
    soft = projected_center_total > _q(limit * b.soft_threshold)

    # Margin-awareness (informational). Unknown -> None when no revenue/spend info.
    margin: Optional[float] = None
    if snapshot.job_revenue_usd is not None or snapshot.job_spend_so_far_usd is not None:
        revenue = snapshot.job_revenue_usd or 0.0
        spend_so_far = snapshot.job_spend_so_far_usd or 0.0
        margin = _q(revenue - spend_so_far - projected)

    def _mk(decision: str, reason: str, tier: Optional[SpendTier]) -> SpendVerdict:
        return SpendVerdict(
            decision=decision,
            reason=reason,
            projected_usd=projected,
            projected_center_total_usd=projected_center_total,
            limit_usd=limit,
            breach=breach,
            soft=soft,
            tier=tier,
            job_margin_if_approved_usd=margin,
        )

    # 1. Auto-approve small spends (auto wins, per §3 ordering).
    if projected <= _q(b.auto_approve_under_usd):
        return _mk("ALLOW", "auto_approve_under_threshold", None)

    # Margin context string for the approval card.
    if margin is None:
        margin_note = "margin_unknown"
    elif margin >= 0:
        margin_note = f"margin_positive({margin:.2f})"
    else:
        margin_note = f"margin_negative({margin:.2f})"

    # 2. Tier routing — amount AND budget state both matter.
    manager_cap = b.manager_under_usd
    if manager_cap is not None and projected < _q(manager_cap) and not breach:
        return _mk(
            "NEEDS_APPROVAL",
            f"manager_tier: projected {projected:.2f} < manager_cap {_q(manager_cap):.2f},"
            f" center_total {projected_center_total:.2f}/{limit:.2f}"
            f"{' SOFT' if soft else ''}; {margin_note}",
            "manager",
        )

    # Otherwise finance: large amount, OR manager_under_usd is NULL, OR breach.
    if breach:
        cause = "budget_breach"
    elif manager_cap is None:
        cause = "no_manager_tier"
    else:
        cause = "large_amount"
    return _mk(
        "NEEDS_APPROVAL",
        f"finance_tier({cause}): center_total {projected_center_total:.2f}/{limit:.2f}"
        f"{' BREACH' if breach else (' SOFT' if soft else '')}; {margin_note}",
        "finance",
    )


# ---------------------------------------------------------------------------
# Compute Allocator (Phase 4.5)  — see CLAUDE.md §3.2
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputeDeclaration:
    """An agent's pre-LLM declaration: what it expects to earn, what it
    expects to burn on inference. Argus uses this to assign a tier."""
    job_id: str
    cost_center_id: str
    expected_revenue_usd: float
    projected_burn_usd: float
    tool_name: Optional[str] = None
    ref: Optional[str] = None


@dataclass(frozen=True)
class ComputeSnapshot:
    """Per-cost-center compute policy + current spent state.

    The tier model names are intentionally not hard-coded in policy —
    they're config-driven so an enterprise can swap Nemotron 3 Ultra
    for a different premium tier without touching code."""
    cost_center_id: str
    ultra_model: str
    base_model: str
    ultra_min_revenue_usd: float
    ultra_min_margin_usd: float
    reject_below_margin_usd: float
    monthly_spent_usd: float = 0.0
    monthly_limit_usd: float = float("inf")


ComputeVerdict = Literal[
    "TIER_ULTRA", "TIER_BASE", "TIER_REJECT", "NEEDS_APPROVAL_MANAGER"
]


@dataclass(frozen=True)
class ComputeDecision:
    verdict: ComputeVerdict
    model: str
    compute_budget_usd: float
    expected_margin_usd: float
    reason: str

    @property
    def is_rejected(self) -> bool:
        return self.verdict == "TIER_REJECT"

    @property
    def needs_approval(self) -> bool:
        return self.verdict == "NEEDS_APPROVAL_MANAGER"

    @property
    def tier_label(self) -> str:
        if self.verdict in ("TIER_ULTRA",):
            return "ultra"
        if self.verdict in ("TIER_BASE",):
            return "base"
        return "reject"


def decide_compute_tier(decl: ComputeDeclaration, snap: ComputeSnapshot) -> ComputeDecision:
    """Assign a compute tier to a job based on its margin profile.

    Rules (in order):
      1. expected_margin < reject_below_margin → REJECT.
      2. monthly_spent + projected_burn > monthly_limit → NEEDS_APPROVAL_MANAGER.
      3. revenue >= ultra_min_revenue AND margin >= ultra_min_margin → ULTRA.
      4. otherwise → BASE.
    """
    expected_margin = decl.expected_revenue_usd - decl.projected_burn_usd

    if expected_margin < snap.reject_below_margin_usd:
        return ComputeDecision(
            "TIER_REJECT",
            model="",
            compute_budget_usd=0.0,
            expected_margin_usd=expected_margin,
            reason=(
                f"negative_margin: expected_margin "
                f"{expected_margin:.2f} < threshold "
                f"{snap.reject_below_margin_usd:.2f}"
            ),
        )

    if snap.monthly_spent_usd + decl.projected_burn_usd > snap.monthly_limit_usd:
        return ComputeDecision(
            "NEEDS_APPROVAL_MANAGER",
            model="",
            compute_budget_usd=decl.projected_burn_usd,
            expected_margin_usd=expected_margin,
            reason=(
                f"monthly_cap_breach: spent {snap.monthly_spent_usd:.2f} + "
                f"projected {decl.projected_burn_usd:.2f} > "
                f"limit {snap.monthly_limit_usd:.2f}"
            ),
        )

    if (
        decl.expected_revenue_usd >= snap.ultra_min_revenue_usd
        and expected_margin >= snap.ultra_min_margin_usd
    ):
        return ComputeDecision(
            "TIER_ULTRA",
            model=snap.ultra_model,
            compute_budget_usd=decl.projected_burn_usd,
            expected_margin_usd=expected_margin,
            reason="ultra_margin_justified",
        )

    return ComputeDecision(
        "TIER_BASE",
        model=snap.base_model,
        compute_budget_usd=decl.projected_burn_usd,
        expected_margin_usd=expected_margin,
        reason="base_default",
    )
