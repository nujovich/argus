"""config.cost_center_for_job — resolve a job's cost center from yaml (§9 dec 3).

Per CLAUDE.md §9 decision 3 the attribution chain is session → job →
cost_center, with the job→cost_center map living in cost_centers.yaml (§9.3 /
cost_centers.sample.yaml). Capture resolves cc from config, NOT a blind default.
"""

from __future__ import annotations

import textwrap

import config as _cfg


def _write_yaml(home, body):
    argus = home / "argus"
    argus.mkdir(parents=True, exist_ok=True)
    (argus / "cost_centers.yaml").write_text(textwrap.dedent(body))


def test_resolves_shorthand_job_map(tmp_hermes_home):
    _write_yaml(tmp_hermes_home, """
        cost_centers:
          saas: {label: SaaS, limit_usd: 200.0}
        jobs:
          job-saas-provisioning: saas
    """)
    assert _cfg.cost_center_for_job("job-saas-provisioning") == "saas"


def test_resolves_extensible_job_map(tmp_hermes_home):
    _write_yaml(tmp_hermes_home, """
        cost_centers:
          services: {label: Svc, limit_usd: 50.0}
        jobs:
          job-x: {cost_center_id: services}
    """)
    assert _cfg.cost_center_for_job("job-x") == "services"


def test_unmapped_job_falls_back_to_default(tmp_hermes_home):
    _write_yaml(tmp_hermes_home, """
        cost_centers:
          saas: {label: SaaS, limit_usd: 200.0}
        jobs:
          other: saas
    """)
    # Not mapped → documented "default" fallback (the cc still resolves through
    # config; the fallback is config's default center, not a hardcoded guess).
    assert _cfg.cost_center_for_job("job-unknown") == "default"


def test_no_yaml_returns_default(tmp_hermes_home):
    assert _cfg.cost_center_for_job("anything") == "default"
