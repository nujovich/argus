"""FastAPI route tests for the dashboard plugin API."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import db


def _client() -> TestClient:
    from dashboard import plugin_api  # noqa: WPS433

    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def test_health(tmp_hermes_home):
    c = _client()
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["plugin"] == "argus"
    assert "db" in body


def test_pnl_empty(tmp_hermes_home):
    r = _client().get("/pnl")
    assert r.status_code == 200
    body = r.json()
    assert body["jobs"] == []
    assert body["total"]["pnl"] == 0


def test_pnl_with_data(tmp_hermes_home):
    db.insert_ledger_row(job_id="j1", kind="revenue", amount_usd=10.0)
    db.insert_ledger_row(job_id="j1", kind="external_spend", amount_usd=3.0)
    r = _client().get("/pnl")
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["pnl"] == 7.0


def test_decide_approval_flow(tmp_hermes_home):
    req_id = db.create_approval_request(
        job_id="j1", cost_center_id="default", projected_usd=5.0, level="manager"
    )
    c = _client()
    r = c.post(
        f"/approvals/{req_id}/decide",
        json={"decision": "approve", "actor": "human:tester"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    # Second decide on same row is now 409.
    r2 = c.post(
        f"/approvals/{req_id}/decide",
        json={"decision": "reject", "actor": "human:tester"},
    )
    assert r2.status_code == 409


def test_stripe_webhook_records_revenue(tmp_hermes_home):
    c = _client()
    r = c.post(
        "/webhooks/stripe",
        json={
            "type": "payment_intent.succeeded",
            "data": {"job_id": "j1", "amount_usd": 42.0, "id": "pi_test_1"},
        },
    )
    assert r.status_code == 200
    assert r.json()["recorded"] == "revenue"
    rows = db.get_pnl_per_job()
    assert rows[0]["revenue"] == 42.0
