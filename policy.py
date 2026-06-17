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
