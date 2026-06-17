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
        )
    return out or dict(DEFAULT_CONFIG)
