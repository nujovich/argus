"""Argus ledger — SQLite WAL DB.

See CLAUDE.md §8 for the schema and §5 for the read-only ATTACH to
hermes-telemetry. This module is the only writer; Policy is pure and
reads via snapshot helpers below.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import config as _cfg  # plugin dir is on sys.path at runtime


_local = threading.local()
_schema_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    path = _cfg.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    path = _get_db_path()
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout must precede journal_mode=WAL — switching a contested fresh
    # DB to WAL needs a brief lock that the default 0-ms timeout won't wait for.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    with _schema_lock:
        _ensure_schema(conn)
    _local.conn = conn
    return conn


def reset_connection_for_tests() -> None:
    """Drop the per-thread connection so the next call reopens a fresh DB."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ledger (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            job_id      TEXT    NOT NULL,
            kind        TEXT    NOT NULL CHECK (kind IN
                          ('revenue', 'llm_cost', 'external_spend')),
            amount_usd  REAL    NOT NULL,
            source      TEXT,
            ref         TEXT,
            session_id  TEXT
        );
        CREATE INDEX IF NOT EXISTS ledger_job_idx ON ledger(job_id);
        CREATE INDEX IF NOT EXISTS ledger_session_idx ON ledger(session_id);

        CREATE TABLE IF NOT EXISTS approval_requests (
            id              TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            job_id          TEXT NOT NULL,
            cost_center_id  TEXT NOT NULL,
            projected_usd   REAL NOT NULL,
            level           TEXT NOT NULL,              -- 'manager' | 'finance'
            status          TEXT NOT NULL CHECK (status IN
                              ('pending','approved','rejected','timeout')),
            decided_at      TEXT,
            decided_by      TEXT,
            reason          TEXT,
            tool_name       TEXT,
            ref             TEXT
        );
        CREATE INDEX IF NOT EXISTS approvals_status_idx ON approval_requests(status);

        CREATE TABLE IF NOT EXISTS audit_trail (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            actor   TEXT NOT NULL,
            event   TEXT NOT NULL,
            payload TEXT
        );
        CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit_trail(ts DESC);
        """
    )


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def insert_ledger_row(
    *,
    job_id: str,
    kind: str,
    amount_usd: float,
    source: Optional[str] = None,
    ref: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO ledger(ts, job_id, kind, amount_usd, source, ref, session_id)"
        " VALUES(?,?,?,?,?,?,?)",
        (_now(), job_id, kind, float(amount_usd), source, ref, session_id),
    )
    return int(cur.lastrowid)


def create_approval_request(
    *,
    job_id: str,
    cost_center_id: str,
    projected_usd: float,
    level: str,
    tool_name: Optional[str] = None,
    ref: Optional[str] = None,
) -> str:
    req_id = uuid.uuid4().hex
    conn = _get_conn()
    conn.execute(
        "INSERT INTO approval_requests(id, created_at, job_id, cost_center_id,"
        " projected_usd, level, status, tool_name, ref) VALUES(?,?,?,?,?,?, 'pending',?,?)",
        (req_id, _now(), job_id, cost_center_id, float(projected_usd), level, tool_name, ref),
    )
    return req_id


def decide_approval(
    req_id: str, *, decision: str, actor: str, reason: Optional[str] = None
) -> bool:
    if decision not in {"approved", "rejected"}:
        raise ValueError(f"invalid decision: {decision}")
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE approval_requests SET status=?, decided_at=?, decided_by=?, reason=?"
        " WHERE id=? AND status='pending'",
        (decision, _now(), actor, reason, req_id),
    )
    return cur.rowcount > 0


def mark_timeout(req_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE approval_requests SET status='timeout', decided_at=?, reason='timeout'"
        " WHERE id=? AND status='pending'",
        (_now(), req_id),
    )
    return cur.rowcount > 0


def log_audit(actor: str, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO audit_trail(ts, actor, event, payload) VALUES(?,?,?,?)",
        (_now(), actor, event, json.dumps(payload) if payload is not None else None),
    )


# ---------------------------------------------------------------------------
# Readers — Policy uses these to build its pure-function snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostCenterSpend:
    cost_center_id: str
    spent_usd: float


def get_approval_status(req_id: str) -> Optional[str]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (req_id,)
    ).fetchone()
    return row["status"] if row else None


def get_pending_approvals() -> List[Dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, created_at, job_id, cost_center_id, projected_usd, level,"
        " status, tool_name, ref FROM approval_requests WHERE status='pending'"
        " ORDER BY created_at ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_approvals(limit: int = 50) -> List[Dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, created_at, job_id, cost_center_id, projected_usd, level,"
        " status, decided_at, decided_by, reason, tool_name, ref"
        " FROM approval_requests ORDER BY created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_audit(limit: int = 50) -> List[Dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT ts, actor, event, payload FROM audit_trail"
        " ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                pass
        out.append(d)
    return out


def get_cost_center_spent(cost_center_id: str, job_ids: Iterable[str]) -> float:
    """Sum of llm_cost + external_spend for jobs mapped to this cost center.

    Caller passes the set of job_ids that belong to the cost center (the
    mapping lives in YAML / agent declarations, not in this DB).
    """
    job_ids = list(job_ids)
    if not job_ids:
        return 0.0
    conn = _get_conn()
    placeholders = ",".join("?" for _ in job_ids)
    row = conn.execute(
        f"SELECT COALESCE(SUM(amount_usd), 0.0) AS s FROM ledger"
        f"  WHERE kind IN ('llm_cost','external_spend') AND job_id IN ({placeholders})",
        job_ids,
    ).fetchone()
    return float(row["s"] or 0.0)


# ---------------------------------------------------------------------------
# P&L — joins llm_cost from hermes-telemetry via read-only ATTACH (A1).
# ---------------------------------------------------------------------------


def get_pnl_per_job() -> List[Dict[str, Any]]:
    """P&L rolled up per job_id.

    Combines Argus's own ledger rows with hermes-telemetry's `runs.cost_usd`
    (joined on session_id) opened read-only. If telemetry.db is absent or
    unreadable, llm_cost simply contributes $0.
    """
    conn = _get_conn()
    tele_path = _cfg.telemetry_db_path()
    have_tele = tele_path.exists()
    try:
        if have_tele:
            conn.execute(
                "ATTACH DATABASE ? AS telemetry",
                (f"file:{tele_path}?mode=ro",),
            )
        rows = conn.execute(_PNL_SQL_WITH_TELE if have_tele else _PNL_SQL).fetchall()
    finally:
        if have_tele:
            try:
                conn.execute("DETACH DATABASE telemetry")
            except sqlite3.OperationalError:
                pass
    return [dict(r) for r in rows]


_PNL_SQL = """
    SELECT job_id,
           SUM(CASE kind WHEN 'revenue'        THEN amount_usd ELSE 0 END) AS revenue,
           SUM(CASE kind WHEN 'llm_cost'       THEN amount_usd ELSE 0 END) AS llm_cost,
           SUM(CASE kind WHEN 'external_spend' THEN amount_usd ELSE 0 END) AS external_spend,
             SUM(CASE kind WHEN 'revenue'        THEN amount_usd ELSE 0 END)
           - SUM(CASE kind WHEN 'llm_cost'       THEN amount_usd ELSE 0 END)
           - SUM(CASE kind WHEN 'external_spend' THEN amount_usd ELSE 0 END) AS pnl
      FROM ledger
     GROUP BY job_id
     ORDER BY job_id
"""

# When telemetry is attached, fold its per-session cost into llm_cost. We use
# ledger.session_id (set by Capture) to bridge job ↔ telemetry.runs.
_PNL_SQL_WITH_TELE = """
    WITH argus AS (
        SELECT job_id,
               SUM(CASE kind WHEN 'revenue'        THEN amount_usd ELSE 0 END) AS revenue,
               SUM(CASE kind WHEN 'llm_cost'       THEN amount_usd ELSE 0 END) AS llm_cost,
               SUM(CASE kind WHEN 'external_spend' THEN amount_usd ELSE 0 END) AS external_spend
          FROM ledger
         GROUP BY job_id
    ),
    tele AS (
        SELECT l.job_id AS job_id,
               COALESCE(SUM(r.cost_usd), 0.0) AS llm_cost_tele
          FROM ledger l
          LEFT JOIN telemetry.runs r ON r.session_id = l.session_id
         WHERE l.session_id IS NOT NULL
         GROUP BY l.job_id
    )
    SELECT a.job_id AS job_id,
           a.revenue AS revenue,
           a.llm_cost + COALESCE(t.llm_cost_tele, 0.0) AS llm_cost,
           a.external_spend AS external_spend,
           a.revenue
             - (a.llm_cost + COALESCE(t.llm_cost_tele, 0.0))
             - a.external_spend AS pnl
      FROM argus a
      LEFT JOIN tele t ON t.job_id = a.job_id
     ORDER BY a.job_id
"""
