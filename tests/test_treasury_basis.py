"""Treasury / P&L share ONE llm_cost basis (MIGRATION_NOTES §8 fix).

Under A1 (CLAUDE.md §5 primary) llm_cost is NOT a ledger row — it is derived
from hermes-telemetry via the read-only ATTACH. The old cash_position summed
llm_cost from the ledger, so under A1 it counted $0 inference cost and
OVER-stated profit (the dangerous direction for a solvency plane). These tests
prove treasury and pnl_by_job now derive llm_cost from the same helper, in BOTH
modes, and never double-count when both bases are somehow present.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import config as _cfg
import db


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


def _pnl_total_llm():
    return round(sum(r["llm_cost"] for r in db.pnl_by_job()), 2)


# ── A1: telemetry present, NO llm_cost ledger rows ──────────────────────────


def test_a1_treasury_counts_telemetry_llm_cost(tmp_hermes_home):
    db.init_db()
    db.register_job("jA1", "default")
    db.link_session("sessA1", "jA1")
    db.append_fact("jA1", "revenue", 100.0, source="stripe")
    # NOTE: no llm_cost ledger row — under A1 the cost lives only in telemetry.
    _make_fixture_telemetry(_cfg.telemetry_db_path(),
                            [("sessA1", "nvidia/nemotron-3-ultra-550b-a55b", 6.0, "t")])

    # The old code would have shown ledger-llm = 0 here (treasury ignored cost).
    assert _pnl_total_llm() == 6.0                      # pnl sees the telemetry cost
    assert db.money_totals()["llm_cost"] == 6.0         # treasury now sees the SAME cost
    assert db.money_totals()["llm_cost"] == _pnl_total_llm()
    # cash_position reflects it: 0 seed + 100 rev - 6 llm - 0 ext = 94 (not 100).
    assert db.cash_position() == 94.0


# ── A2: no telemetry.db, llm_cost ledger rows present ───────────────────────


def test_a2_treasury_matches_pnl(tmp_hermes_home):
    db.init_db()
    db.register_job("jA2", "default")
    db.append_fact("jA2", "revenue", 100.0, source="stripe")
    db.append_fact("jA2", "llm_cost", 6.0)              # A2: real ledger row
    assert not _cfg.telemetry_db_path().exists()         # A2 mode

    assert _pnl_total_llm() == 6.0
    assert db.money_totals()["llm_cost"] == 6.0
    assert db.money_totals()["llm_cost"] == _pnl_total_llm()
    assert db.cash_position() == 94.0


# ── No double-count: BOTH bases present → pick ONE (A1 preferred) ───────────


def test_no_double_count_prefers_a1(tmp_hermes_home):
    db.init_db()
    db.register_job("jBoth", "default")
    db.link_session("sessBoth", "jBoth")
    db.append_fact("jBoth", "revenue", 100.0, source="stripe")
    db.append_fact("jBoth", "llm_cost", 5.0)            # stray A2 row
    _make_fixture_telemetry(_cfg.telemetry_db_path(),
                            [("sessBoth", "nvidia/nemotron-3-base-9b", 6.0, "t")])

    # Telemetry present → A1 basis wins; the $5 ledger row is IGNORED, NOT summed.
    assert db.money_totals()["llm_cost"] == 6.0          # not 11.0
    assert _pnl_total_llm() == 6.0                        # pnl agrees, also not 11.0
    assert db.money_totals()["llm_cost"] == _pnl_total_llm()
    # cash_position uses the single basis: 100 - 6 = 94 (not 89).
    assert db.cash_position() == 94.0


# ── general invariant: across many jobs, treasury == sum of per-job pnl ──────


def test_treasury_llm_equals_pnl_sum_multi_job(tmp_hermes_home):
    db.init_db()
    for jid, sess, rev in (("j1", "s1", 50.0), ("j2", "s2", 30.0)):
        db.register_job(jid, "default")
        db.link_session(sess, jid)
        db.append_fact(jid, "revenue", rev, source="stripe")
    _make_fixture_telemetry(_cfg.telemetry_db_path(), [
        ("s1", "m", 4.0, "t"),
        ("s2", "m", 1.5, "t"),
        ("orphan", "m", 99.0, "t"),   # not linked → must NOT count anywhere
    ])

    assert db.money_totals()["llm_cost"] == 5.5          # 4.0 + 1.5, orphan excluded
    assert _pnl_total_llm() == 5.5
    assert db.money_totals()["llm_cost"] == _pnl_total_llm()
