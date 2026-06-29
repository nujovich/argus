"""Durable spend-declaration CRUD + attribution getters (CLAUDE.md §2, §8).

The store stays PASSIVE: plain inserts/selects/updates, no policy. These tests
prove the declaration survives a fresh connection (durable, cross-process) and
that Enforcement reads it through find_open_declaration — the gap MIGRATION_NOTES
§6 flagged.
"""

from __future__ import annotations

import db
import enforcement


# ── plain CRUD ───────────────────────────────────────────────────────────────


def test_insert_then_find_open_by_session(tmp_hermes_home):
    db.init_db()
    decl_id = db.insert_declaration(
        job_id="jobX", session_id="sessX", projected_usd=12.5, ref="r1"
    )
    assert isinstance(decl_id, int) and decl_id > 0

    row = db.find_open_declaration(session_id="sessX")
    assert row is not None
    assert row["job_id"] == "jobX"
    assert row["projected_usd"] == 12.5
    assert row["ref"] == "r1"
    assert row["consumed_at"] is None


def test_find_open_by_job_id(tmp_hermes_home):
    db.init_db()
    db.insert_declaration(job_id="jobY", session_id=None, projected_usd=4.0)
    row = db.find_open_declaration(job_id="jobY")
    assert row is not None and row["job_id"] == "jobY"


def test_declaration_survives_fresh_connection(tmp_hermes_home):
    db.init_db()
    db.insert_declaration(job_id="jobZ", session_id="sessZ", projected_usd=9.0)
    # Drop the per-thread connection — a brand-new connection must still see it
    # (durable, not an in-process cache).
    db.reset_connection_for_tests()
    db.init_db()
    row = db.find_open_declaration(session_id="sessZ")
    assert row is not None and row["projected_usd"] == 9.0


def test_mark_consumed_then_no_longer_open(tmp_hermes_home):
    db.init_db()
    decl_id = db.insert_declaration(job_id="jc", session_id="sc", projected_usd=3.0)
    assert db.mark_declaration_consumed(decl_id) is True
    # Consumed → not returned as open.
    assert db.find_open_declaration(session_id="sc") is None
    # Second consume is a no-op (idempotent).
    assert db.mark_declaration_consumed(decl_id) is False


def test_find_open_returns_latest_open(tmp_hermes_home):
    db.init_db()
    first = db.insert_declaration(job_id="jm", session_id="sm", projected_usd=1.0)
    db.insert_declaration(job_id="jm", session_id="sm", projected_usd=2.0)
    row = db.find_open_declaration(session_id="sm")
    # Newest open declaration wins.
    assert row["projected_usd"] == 2.0
    # Consuming the latest falls back to the earlier still-open one.
    db.mark_declaration_consumed(row["id"])
    row2 = db.find_open_declaration(session_id="sm")
    assert row2 is not None and row2["id"] == first


# ── attribution getters (the missing chain readers) ─────────────────────────


def test_get_job_for_session(tmp_hermes_home):
    db.init_db()
    db.register_job("jobA", "ccA")
    db.link_session("sessA", "jobA")
    assert db.get_job_for_session("sessA") == "jobA"
    assert db.get_job_for_session("nope") is None


def test_get_cost_center_for_job(tmp_hermes_home):
    db.init_db()
    db.register_job("jobB", "ccB")
    assert db.get_cost_center_for_job("jobB") == "ccB"
    assert db.get_cost_center_for_job("nope") is None


# ── Enforcement reads the durable declaration (the flagged gap, closed) ──────


def test_enforcement_find_open_declaration_via_declare_spend(tmp_hermes_home):
    db.init_db()
    # declare_spend is now durable: it registers the job→cc and writes a row.
    enforcement.declare_spend("jobE", 42.0, cost_center_id="ccE", session_id="sessE")
    decl = enforcement._lookup_declaration("sessE")
    assert decl is not None
    assert decl["job_id"] == "jobE"
    assert decl["cost_center_id"] == "ccE"      # resolved via the jobs bridge
    assert decl["projected_usd"] == 42.0
