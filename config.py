"""Argus configuration — paths and cost-center / budget loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import yaml


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def argus_dir() -> Path:
    return hermes_home() / "argus"


def db_path() -> Path:
    return argus_dir() / "argus.db"


def telemetry_db_path() -> Path:
    # See CLAUDE.md §4. Argus only reads this file, never writes.
    return hermes_home() / "telemetry" / "telemetry.db"


def cost_centers_yaml_path() -> Path:
    return argus_dir() / "cost_centers.yaml"


@dataclass(frozen=True)
class Budget:
    cost_center_id: str
    label: str
    limit_usd: float
    soft_threshold: float = 0.8
    auto_approve_under_usd: float = 0.0
    manager_under_usd: Optional[float] = None
    # Phase 4.5 — compute tier policy fields, optional per cost center.
    # When unset, the cost center behaves cash-only (existing v1 behaviour).
    ultra_model: Optional[str] = None
    base_model: Optional[str] = None
    ultra_min_revenue_usd: Optional[float] = None
    ultra_min_margin_usd: Optional[float] = None
    reject_below_margin_usd: Optional[float] = None


# Default config seeded if cost_centers.yaml is missing — makes the demo
# work out of the box. Tiers: auto ≤ $1, manager ≤ $10, finance > $10.
DEFAULT_CONFIG: Dict[str, Budget] = {
    "default": Budget(
        cost_center_id="default",
        label="Default cost center",
        limit_usd=50.0,
        auto_approve_under_usd=1.0,
        manager_under_usd=10.0,
    ),
}


def seed_capital() -> float:
    """Starting cash on the treasury balance sheet (CLAUDE.md §9.2 close).
    Env ``ARGUS_SEED_CAPITAL`` wins, then a top-level ``seed_capital:`` in
    cost_centers.yaml, else 0.0. Treasury = seed + revenue − all spend."""
    env = os.environ.get("ARGUS_SEED_CAPITAL")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass
    path = cost_centers_yaml_path()
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
        if raw.get("seed_capital") is not None:
            try:
                return float(raw["seed_capital"])
            except (TypeError, ValueError):
                pass
    return 0.0


def stripe_webhook_secret() -> Optional[str]:
    """The Stripe webhook signing secret (test-mode per §10). Read from env
    ``ARGUS_STRIPE_WEBHOOK_SECRET`` / ``STRIPE_WEBHOOK_SECRET``. None when
    unset → the webhook fails closed (cannot verify → reject)."""
    return (
        os.environ.get("ARGUS_STRIPE_WEBHOOK_SECRET")
        or os.environ.get("STRIPE_WEBHOOK_SECRET")
        or None
    )


def cost_center_for_job(job_id: str, default: str = "default") -> str:
    """Resolve a job's cost center from cost_centers.yaml's ``jobs:`` map
    (CLAUDE.md §9 decision 3 / §9.3). Accepts either shorthand
    ``job_id: cost_center_id`` or extensible ``job_id: {cost_center_id: ...}``.

    Returns the configured center, or ``default`` when the job isn't mapped /
    no config exists. The point (vs. a blind default) is that a configured job
    lands on its real center; Capture calls this instead of guessing."""
    path = cost_centers_yaml_path()
    if not path.exists():
        return default
    raw = yaml.safe_load(path.read_text()) or {}
    jv = (raw.get("jobs") or {}).get(job_id)
    if jv is None:
        return default
    if isinstance(jv, str):
        return jv
    if isinstance(jv, dict):
        return jv.get("cost_center_id") or default
    return default


def load_budgets() -> Dict[str, Budget]:
    path = cost_centers_yaml_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text()) or {}
    out: Dict[str, Budget] = {}
    for cc_id, cfg in (raw.get("cost_centers") or {}).items():
        out[cc_id] = Budget(
            cost_center_id=cc_id,
            label=cfg.get("label", cc_id),
            limit_usd=float(cfg["limit_usd"]),
            soft_threshold=float(cfg.get("soft_threshold", 0.8)),
            auto_approve_under_usd=float(cfg.get("auto_approve_under_usd", 0.0)),
            manager_under_usd=(
                float(cfg["manager_under_usd"])
                if cfg.get("manager_under_usd") is not None
                else None
            ),
            ultra_model=cfg.get("ultra_model"),
            base_model=cfg.get("base_model"),
            ultra_min_revenue_usd=(
                float(cfg["ultra_min_revenue_usd"])
                if cfg.get("ultra_min_revenue_usd") is not None else None
            ),
            ultra_min_margin_usd=(
                float(cfg["ultra_min_margin_usd"])
                if cfg.get("ultra_min_margin_usd") is not None else None
            ),
            reject_below_margin_usd=(
                float(cfg["reject_below_margin_usd"])
                if cfg.get("reject_below_margin_usd") is not None else None
            ),
        )
    return out or dict(DEFAULT_CONFIG)
