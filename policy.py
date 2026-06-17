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
