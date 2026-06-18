"""Ledger layer tests — schema, bridge, P&L (A1/A2), approvals, budgets, RO safety.

The Ledger is read by every other layer, so this proves the passive store
end to end. See CLAUDE.md §2 / §8 and MIGRATION_NOTES.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config as _cfg
import db


# ── schema / idempotency / WAL ──────────────────────────────────────────────


def test_schema_idempotent_and_wal(tmp_hermes_home):
    conn = db.init_db()
    # WAL is on.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    # All §8 tables + the attribution bridge exist.
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "ledger", "approval_requests", "audit_trail", "auth_tokens",
        "compute_allocations", "cost_centers", "budgets", "jobs", "job_sessions",
    } <= tables
    # Re-applying the schema is a no-op (idempotent) and preserves data.
    db.append_fact("j1", "revenue", 5.0)
    db.migrate()
    db.migrate()
    assert db.pnl_by_job()[0]["revenue"] == 5.0


def test_append_fact_rejects_bad_kind(tmp_hermes_home):
    db.init_db()
    with pytest.raises(ValueError):
        db.append_fact("j1", "bribe", 1.0)


def test_money_quantized_to_cents_on_write(tmp_hermes_home):
    db.init_db()
    db.register_job("j1", "cc1")
    db.append_fact("j1", "revenue", 10.005)      # half-up -> 10.01
    db.append_fact("j1", "external_spend", 0.014)  # -> 0.01
    row = db.pnl_by_job()[0]
    assert row["revenue"] == 10.01
    assert row["external_spend"] == 0.01


# ── append_fact + pnl_by_job math (multi-job, multi-kind) ───────────────────


def test_pnl_math_multi_job_multi_kind(tmp_hermes_home):
    db.init_db()
    # Job A: profitable. Job B: revenue only. Job C: pure spend (loss).
    db.append_fact("A", "revenue", 200.0)
    db.append_fact("A", "external_spend", 15.0)
    db.append_fact("A", "llm_cost", 5.0)        # A2-style llm_cost row
    db.append_fact("B", "revenue", 3.0)
    db.append_fact("C", "external_spend", 7.5)

    pnl = {r["job_id"]: r for r in db.pnl_by_job()}
    assert pnl["A"]["pnl"] == 180.0            # 200 - 15 - 5
    assert pnl["B"]["pnl"] == 3.0
    assert pnl["C"]["pnl"] == -7.5


# ── A1 ATTACH join via job_sessions, then A2 fallback ───────────────────────


def _make_fixture_telemetry(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tconn = sqlite3.connect(str(path))
    tconn.execute(
        "CREATE TABLE runs (session_id TEXT PRIMARY KEY, model TEXT,"
        " cost_usd REAL, started_at TEXT)"
    )
    tconn.executemany(
        "INSERT INTO runs(session_id, model, cost_usd, started_at) VALUES(?,?,?,?)",
        rows,
    )
    tconn.commit()
    tconn.close()


def test_a1_attach_join_then_a2_fallback(tmp_hermes_home):
    db.init_db()
    # Two jobs, each with revenue in the ledger and sessions linked.
    db.register_job("jobA", "ccA")
    db.register_job("jobB", "ccB")
    db.append_fact("jobA", "revenue", 200.0)
    db.append_fact("jobB", "revenue", 3.0)
    db.link_session("sessA", "jobA")
    db.link_session("sessB", "jobB")

    tele_path = _cfg.telemetry_db_path()
    _make_fixture_telemetry(tele_path, [
        ("sessA", "nvidia/nemotron-3-ultra-550b-a55b", 15.0, "t"),
        ("sessB", "nvidia/nemotron-3-base-9b", 0.30, "t"),
        ("orphan", "x", 99.0, "t"),  # not linked to any job -> must NOT count
    ])

    # A1: telemetry cost attributes to the right job via job_sessions.
    pnl = {r["job_id"]: r for r in db.pnl_by_job()}
    assert pnl["jobA"]["llm_cost"] == 15.0
    assert pnl["jobA"]["pnl"] == 185.0          # 200 - 15
    assert pnl["jobB"]["llm_cost"] == 0.30
    assert pnl["jobB"]["pnl"] == 2.70
    # The orphan session's $99 never leaks into any job's P&L.
    assert "orphan" not in pnl

    # A2: delete telemetry.db -> fallback still returns P&L (llm_cost -> 0).
    tele_path.unlink()
    pnl2 = {r["job_id"]: r for r in db.pnl_by_job()}
    assert pnl2["jobA"]["llm_cost"] == 0.0
    assert pnl2["jobA"]["pnl"] == 200.0
    assert pnl2["jobB"]["pnl"] == 3.0


def test_read_only_safety_never_writes_telemetry(tmp_hermes_home):
    db.init_db()
    db.register_job("jobA", "ccA")
    db.append_fact("jobA", "revenue", 10.0)
    db.link_session("sessA", "jobA")

    tele_path = _cfg.telemetry_db_path()
    _make_fixture_telemetry(tele_path, [("sessA", "m", 1.0, "t")])

    before_mtime = tele_path.stat().st_mtime_ns
    before_rows = sqlite3.connect(str(tele_path)).execute(
        "SELECT COUNT(*) FROM runs"
    ).fetchone()[0]

    # Run the P&L query many times — it ATTACHes telemetry read-only each time.
    for _ in range(5):
        db.pnl_by_job()

    after_rows = sqlite3.connect(str(tele_path)).execute(
        "SELECT COUNT(*) FROM runs"
    ).fetchone()[0]
    assert after_rows == before_rows == 1
    # No write landed: row count unchanged and file content untouched.
    assert tele_path.stat().st_mtime_ns == before_mtime

    # And a direct write attempt through a mode=ro ATTACH is refused by SQLite.
    conn = db.init_db()
    conn.execute("ATTACH DATABASE ? AS t_ro", (f"file:{tele_path}?mode=ro",))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t_ro.runs(session_id) VALUES('x')")
    finally:
        conn.execute("DETACH DATABASE t_ro")


# ── approval lifecycle ──────────────────────────────────────────────────────


@pytest.mark.parametrize("terminal", ["approved", "rejected", "timeout"])
def test_approval_lifecycle_transitions(tmp_hermes_home, terminal):
    db.init_db()
    aid = db.create_approval("j1", "cc1", 12.50)
    assert db.read_approval(aid)["status"] == "pending"

    assert db.decide_approval(aid, terminal, "human:test", "because") is True
    row = db.read_approval(aid)
    assert row["status"] == terminal
    assert row["decided_by"] == "human:test"

    # A second decision on an already-decided row is a no-op.
    assert db.decide_approval(aid, "approved", "human:other") is False
    assert db.read_approval(aid)["status"] == terminal


def test_decide_approval_legacy_kwargs_still_work(tmp_hermes_home):
    # The dashboard calls decide_approval(req_id, decision=, actor=, reason=).
    db.init_db()
    aid = db.create_approval("j1", "cc1", 1.0)
    assert db.decide_approval(aid, decision="approved", actor="human:x") is True
    assert db.read_approval(aid)["status"] == "approved"


# ── budget rollup via the cost-center bridge ────────────────────────────────


def test_budget_rollup_sums_to_cost_center(tmp_hermes_home):
    db.init_db()
    db.seed_from_config()  # seeds cost_centers + budgets from sample config if present
    # Two jobs in the same cost center, one in another.
    db.register_job("j1", "ccX")
    db.register_job("j2", "ccX")
    db.register_job("j3", "ccY")
    db.upsert_budget("ccX", limit_usd=100.0)

    db.append_fact("j1", "external_spend", 10.0)
    db.append_fact("j2", "external_spend", 25.0)
    db.append_fact("j2", "llm_cost", 5.0)
    db.append_fact("j1", "revenue", 999.0)       # revenue must NOT count as spend
    db.append_fact("j3", "external_spend", 50.0)  # different center

    snap = db.ledger_snapshot("ccX")
    assert snap["spent_usd"] == 40.0            # 10 + 25 + 5 (revenue excluded)
    assert snap["limit_usd"] == 100.0
    assert snap["remaining_usd"] == 60.0
    # ccY isolated.
    assert db.ledger_snapshot("ccY")["spent_usd"] == 50.0


def test_seed_from_config_idempotent(tmp_hermes_home):
    db.init_db()
    # Write a tiny config into the isolated HERMES_HOME and seed twice.
    cfg = _cfg.cost_centers_yaml_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "cost_centers:\n"
        "  ccX:\n    label: X\n    limit_usd: 50.0\n    manager_under_usd: 10.0\n"
        "jobs:\n  jobA: ccX\n"
    )
    first = db.seed_from_config()
    second = db.seed_from_config()
    assert first == second == {"cost_centers": 1, "budgets": 1, "jobs": 1}
    assert db.budget_for("ccX")["limit_usd"] == 50.0
    # jobA was mapped via the §9.3 job→cost_center map.
    snap = db.ledger_snapshot("ccX")
    assert snap["limit_usd"] == 50.0
