"""Standalone dashboard plumbing — /jobs, /state, and the standalone ASGI app.

Backend glue only (no gating logic). /jobs is the fleet superset (every ledger
job + allocation-only jobs); /state is the one-call SPA snapshot; standalone.py
serves the SPA same-origin with permissive localhost CORS and optional Bearer auth.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import db


def _client() -> TestClient:
    from dashboard import plugin_api  # noqa: WPS433

    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


# ── /jobs — every ledger job, including cash-only (no allocation) ───────────


def test_jobs_includes_cash_only_job(tmp_hermes_home):
    db.init_db()
    # A cash-only job: ledger activity, NO compute allocation (the reject /
    # self-correct beat spends cash without ever getting a tier).
    db.register_job("cash-only", "default")
    db.append_fact("cash-only", "revenue", 10.0, source="stripe")
    db.append_fact("cash-only", "external_spend", 4.0, source="stripe")

    # A compute job WITH an allocation.
    db.register_job("compute-job", "saas")
    db.insert_compute_allocation(
        job_id="compute-job", cost_center_id="saas", tier="ultra",
        model="nvidia/nemotron-3-ultra-550b-a55b", compute_budget_usd=15.0,
        expected_revenue_usd=200.0, expected_margin_usd=185.0,
    )

    items = {j["job_id"]: j for j in _client().get("/jobs").json()["items"]}

    # cash-only MUST appear, with null allocation and correct P&L.
    assert "cash-only" in items
    co = items["cash-only"]
    assert co["allocation"] is None
    assert co["cost_center_id"] == "default"
    assert co["revenue"] == 10.0
    assert co["external_spend"] == 4.0
    assert co["margin"] == 6.0

    # compute-job appears too (superset of /compute/fleet), with its allocation.
    assert "compute-job" in items
    cj = items["compute-job"]
    assert cj["allocation"] is not None
    assert cj["allocation"]["tier"] == "ultra"
    assert cj["status"] == "active"


def test_jobs_is_superset_of_compute_fleet(tmp_hermes_home):
    db.init_db()
    db.register_job("cash-only", "default")
    db.append_fact("cash-only", "external_spend", 2.0, source="stripe")
    db.register_job("compute-job", "default")
    db.insert_compute_allocation(
        job_id="compute-job", cost_center_id="default", tier="base",
        model="m", compute_budget_usd=1.0, expected_revenue_usd=3.0,
        expected_margin_usd=2.0,
    )
    c = _client()
    jobs = {j["job_id"] for j in c.get("/jobs").json()["items"]}
    fleet = {f["job_id"] for f in c.get("/compute/fleet").json()["items"]}
    assert fleet <= jobs                      # /jobs is a superset
    assert "cash-only" in jobs and "cash-only" not in fleet


# ── /state — one-call snapshot + eye_state ──────────────────────────────────


def test_state_has_all_sections_and_watching_when_idle(tmp_hermes_home):
    db.init_db()
    db.append_fact("j", "revenue", 5.0, source="stripe")
    st = _client().get("/state").json()
    for section in ("pnl", "treasury", "approvals", "audit", "fleet", "tokens", "eye_state"):
        assert section in st
    assert st["eye_state"] == "watching"      # no pending approvals
    assert st["approvals"]["pending"] == []
    assert "jobs" in st["pnl"] and "total" in st["pnl"]
    assert "cash_position" in st["treasury"]
    assert any(f["job_id"] == "j" for f in st["fleet"]["items"])


def test_state_eye_flips_to_holding_on_pending_approval(tmp_hermes_home):
    db.init_db()
    db.create_approval_request(
        job_id="j", cost_center_id="default", projected_usd=5.0, level="manager"
    )
    st = _client().get("/state").json()
    assert st["eye_state"] == "holding"
    assert len(st["approvals"]["pending"]) == 1


def test_state_audit_limit_query_param(tmp_hermes_home):
    db.init_db()
    for i in range(10):
        db.append_audit("system", "tick", {"i": i})
    st = _client().get("/state?audit_limit=3").json()
    assert len(st["audit"]["items"]) == 3


# ── standalone app — prefix mount + same-origin static + CORS + auth ────────


def _standalone():
    import standalone  # noqa: WPS433
    return standalone.build_app()


def test_standalone_mounts_router_under_prefix_and_serves_index(tmp_hermes_home):
    c = TestClient(_standalone())
    # API under the Hermes prefix (which Hermes supplies in-process; standalone
    # adds it itself).
    r = c.get("/api/plugins/argus/health")
    assert r.status_code == 200 and r.json()["plugin"] == "argus"
    # SPA served same-origin at "/".
    idx = c.get("/")
    assert idx.status_code == 200
    assert "argus" in idx.text.lower()


def test_standalone_cors_preflight_from_localhost_dev_origin(tmp_hermes_home):
    c = TestClient(_standalone())
    r = c.options(
        "/api/plugins/argus/state",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_standalone_auth_off_by_default(tmp_hermes_home, monkeypatch):
    monkeypatch.delenv("ARGUS_DASHBOARD_TOKEN", raising=False)
    c = TestClient(_standalone())
    assert c.get("/api/plugins/argus/health").status_code == 200


def test_standalone_auth_required_when_token_set(tmp_hermes_home, monkeypatch):
    monkeypatch.setenv("ARGUS_DASHBOARD_TOKEN", "sekret")
    c = TestClient(_standalone())
    # No token → 401.
    assert c.get("/api/plugins/argus/health").status_code == 401
    # Correct Bearer → 200.
    ok = c.get("/api/plugins/argus/health", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200
    # Static SPA stays reachable (the SPA itself sends the Bearer on API calls).
    assert c.get("/").status_code == 200
