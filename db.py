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
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False, uri=True
    )
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

        -- Defense in depth: every ALLOW issues a short-lived single-use
        -- token. The agent must include token in metadata.argus_auth_token
        -- on its next Stripe call; the hook validates against this table.
        -- Without a valid token, Stripe spends are BLOCKED — even if the
        -- agent never called argus_request_spend first. See CLAUDE.md §6.
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token           TEXT PRIMARY KEY,
            issued_at       TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            job_id          TEXT NOT NULL,
            cost_center_id  TEXT NOT NULL,
            amount_usd      REAL NOT NULL,
            tolerance_pct   REAL NOT NULL DEFAULT 0.10,
            approval_id     TEXT,
            consumed_at     TEXT,
            consumed_by_ref TEXT
        );
        CREATE INDEX IF NOT EXISTS auth_tokens_expires_idx ON auth_tokens(expires_at);

        -- Compute Allocator (Phase 4.5): the per-job record of which
        -- Nemotron tier was assigned, what budget, and the live state.
        -- See CLAUDE.md §2 / §3.2 / §3.3.
        CREATE TABLE IF NOT EXISTS compute_allocations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            job_id          TEXT NOT NULL,
            cost_center_id  TEXT NOT NULL,
            tier            TEXT NOT NULL CHECK (tier IN ('ultra','base','reject')),
            model           TEXT NOT NULL,
            compute_budget_usd  REAL NOT NULL,
            expected_revenue_usd REAL,
            expected_margin_usd  REAL,
            status          TEXT NOT NULL CHECK (status IN
                              ('active','downgraded','killed','done')),
            downgrade_reason TEXT,
            session_id      TEXT,
            auth_token      TEXT
        );
        CREATE INDEX IF NOT EXISTS compute_alloc_job_idx
            ON compute_allocations(job_id);
        CREATE INDEX IF NOT EXISTS compute_alloc_status_idx
            ON compute_allocations(status);
        """
    )
    # Idempotent schema migrations — add columns that may not exist on
    # an older DB without breaking startup.
    for col_sql in (
        "ALTER TABLE compute_allocations ADD COLUMN actual_model TEXT",
        "ALTER TABLE compute_allocations ADD COLUMN integrity_status TEXT",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.executescript(
        """
        -- guarantee schema_version exists for future migrations
        CREATE TABLE IF NOT EXISTS _argus_schema_version (
            version INTEGER PRIMARY KEY
        );
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
# Auth tokens — defense in depth, see CLAUDE.md §6
# ---------------------------------------------------------------------------


def _now_epoch() -> float:
    return time.time()


def issue_auth_token(
    *,
    job_id: str,
    cost_center_id: str,
    amount_usd: float,
    ttl_seconds: int = 60,
    tolerance_pct: float = 0.10,
    approval_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Mint a short-lived single-use auth token. The agent must echo it in
    the next Stripe call's ``metadata.argus_auth_token`` for the spend to
    pass the in-process backstop check."""
    token = uuid.uuid4().hex
    issued = _now_epoch()
    expires = issued + ttl_seconds
    conn = _get_conn()
    conn.execute(
        "INSERT INTO auth_tokens(token, issued_at, expires_at, job_id,"
        " cost_center_id, amount_usd, tolerance_pct, approval_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            token,
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(issued)),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires)),
            job_id,
            cost_center_id,
            float(amount_usd),
            float(tolerance_pct),
            approval_id,
        ),
    )
    return {
        "auth_token": token,
        "expires_in": ttl_seconds,
        "amount_usd": float(amount_usd),
        "job_id": job_id,
        "cost_center_id": cost_center_id,
        "tolerance_pct": tolerance_pct,
    }


@dataclass(frozen=True)
class AuthTokenCheck:
    valid: bool
    reason: str
    token_row: Optional[Dict[str, Any]] = None


def validate_and_consume_auth_token(
    token: str,
    *,
    actual_amount_usd: float,
    actual_job_id: Optional[str] = None,
    ref: Optional[str] = None,
) -> AuthTokenCheck:
    """Look up + atomically consume an auth token. Reject if expired,
    already consumed, or if the actual charge doesn't match (job_id +
    amount within tolerance)."""
    if not token:
        return AuthTokenCheck(False, "missing_token")
    conn = _get_conn()
    row = conn.execute(
        "SELECT token, issued_at, expires_at, job_id, cost_center_id,"
        " amount_usd, tolerance_pct, consumed_at"
        " FROM auth_tokens WHERE token=?",
        (token,),
    ).fetchone()
    if row is None:
        return AuthTokenCheck(False, "unknown_token")
    d = dict(row)
    if d["consumed_at"] is not None:
        return AuthTokenCheck(False, "already_consumed", d)

    # Expiry: compare ISO strings; ISO Zulu lexsort is correct here.
    now_iso = _now()
    if now_iso > d["expires_at"]:
        return AuthTokenCheck(False, "expired", d)

    if actual_job_id is not None and actual_job_id != d["job_id"]:
        return AuthTokenCheck(False, f"job_mismatch:{actual_job_id}_vs_{d['job_id']}", d)

    expected = d["amount_usd"]
    tol = d["tolerance_pct"]
    if expected > 0:
        delta_pct = abs(actual_amount_usd - expected) / expected
        if delta_pct > tol:
            return AuthTokenCheck(
                False,
                f"amount_mismatch:{actual_amount_usd:.2f}_vs_{expected:.2f}(tol={tol:.0%})",
                d,
            )

    cur = conn.execute(
        "UPDATE auth_tokens SET consumed_at=?, consumed_by_ref=?"
        " WHERE token=? AND consumed_at IS NULL",
        (now_iso, ref, token),
    )
    if cur.rowcount == 0:
        # Race: another consumer just took it.
        return AuthTokenCheck(False, "race_consumed", d)
    return AuthTokenCheck(True, "ok", d)


def get_active_token_count() -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM auth_tokens"
        " WHERE consumed_at IS NULL AND expires_at > ?",
        (_now(),),
    ).fetchone()
    return int(row["c"] or 0)


# ---------------------------------------------------------------------------
# Compute Allocator (Phase 4.5)
# ---------------------------------------------------------------------------


def insert_compute_allocation(
    *,
    job_id: str,
    cost_center_id: str,
    tier: str,
    model: str,
    compute_budget_usd: float,
    expected_revenue_usd: Optional[float],
    expected_margin_usd: Optional[float],
    session_id: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO compute_allocations(ts, job_id, cost_center_id, tier,"
        " model, compute_budget_usd, expected_revenue_usd, expected_margin_usd,"
        " status, session_id, auth_token)"
        " VALUES(?,?,?,?,?,?,?,?, ?, ?, ?)",
        (
            _now(), job_id, cost_center_id, tier, model,
            float(compute_budget_usd),
            expected_revenue_usd,
            expected_margin_usd,
            "active" if tier != "reject" else "killed",
            session_id, auth_token,
        ),
    )
    return int(cur.lastrowid)


def get_compute_allocations(job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _get_conn()
    if job_id:
        rows = conn.execute(
            "SELECT * FROM compute_allocations WHERE job_id=? ORDER BY id DESC",
            (job_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM compute_allocations ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_latest_active_allocation(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM compute_allocations WHERE job_id=? AND status='active'"
        " ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def update_allocation_status(alloc_id: int, status: str,
                             downgrade_reason: Optional[str] = None) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE compute_allocations SET status=?, downgrade_reason=COALESCE(?, downgrade_reason)"
        " WHERE id=?",
        (status, downgrade_reason, alloc_id),
    )


def set_actual_model(alloc_id: int, actual_model: str) -> None:
    """Record what model was actually used for an allocation. In the real
    flow this comes from the read-only ATTACH to hermes-telemetry's
    runs.model. The deterministic demo records it directly to demonstrate
    the silent-fallback case end-to-end."""
    conn = _get_conn()
    conn.execute(
        "UPDATE compute_allocations SET actual_model=? WHERE id=?",
        (actual_model, alloc_id),
    )


def run_compute_integrity_sweep() -> List[Dict[str, Any]]:
    """Compare each active allocation's authorized model against the
    actual model used (from compute_allocations.actual_model or from a
    telemetry session join). Returns the list of violations found and
    writes a compute_integrity_violation audit row for each."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, job_id, cost_center_id, tier, model AS authorized_model,"
        " actual_model, session_id, status"
        " FROM compute_allocations"
        " WHERE tier != 'reject' AND status='active'"
    ).fetchall()

    violations: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        observed = d.get("actual_model")
        # If we have a session_id and the demo didn't pre-populate
        # actual_model, peek at telemetry.runs (best-effort).
        if not observed and d.get("session_id"):
            try:
                import config as _cfg
                tele = _cfg.telemetry_db_path()
                if tele.exists():
                    conn.execute(
                        "ATTACH DATABASE ? AS telemetry",
                        (f"file:{tele}?mode=ro",),
                    )
                    trow = conn.execute(
                        "SELECT model FROM telemetry.runs WHERE session_id=?",
                        (d["session_id"],),
                    ).fetchone()
                    if trow and trow["model"]:
                        observed = trow["model"]
                    conn.execute("DETACH DATABASE telemetry")
            except sqlite3.Error:
                try:
                    conn.execute("DETACH DATABASE telemetry")
                except Exception:
                    pass

        if observed and observed != d["authorized_model"]:
            payload = {
                "allocation_id": d["id"],
                "job_id": d["job_id"],
                "cost_center_id": d["cost_center_id"],
                "authorized_model": d["authorized_model"],
                "observed_model": observed,
                "tier_authorized": d["tier"],
            }
            conn.execute(
                "UPDATE compute_allocations SET integrity_status='violation'"
                " WHERE id=?",
                (d["id"],),
            )
            log_audit("system", "compute_integrity_violation", payload)
            violations.append(payload)
        elif observed:
            conn.execute(
                "UPDATE compute_allocations SET integrity_status='ok'"
                " WHERE id=? AND COALESCE(integrity_status,'') != 'violation'",
                (d["id"],),
            )
    log_audit("system", "compute_integrity_sweep", {"violations": len(violations)})
    return violations


def get_job_burn(job_id: str) -> float:
    """Sum of llm_cost rows for a given job. Used by the throttle check."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0.0) AS s FROM ledger"
        " WHERE kind='llm_cost' AND job_id=?",
        (job_id,),
    ).fetchone()
    return float(row["s"] or 0.0)


def get_job_revenue(job_id: str) -> float:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0.0) AS s FROM ledger"
        " WHERE kind='revenue' AND job_id=?",
        (job_id,),
    ).fetchone()
    return float(row["s"] or 0.0)


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
    attached = False
    if tele_path.exists():
        try:
            conn.execute(
                "ATTACH DATABASE ? AS telemetry",
                (f"file:{tele_path}?mode=ro",),
            )
            attached = True
        except sqlite3.Error:
            # Telemetry DB exists but couldn't be attached (locked, wrong
            # schema, URI not supported by this sqlite build). Fall back to
            # the no-telemetry query — better to show $0 LLM cost than 500.
            attached = False
    try:
        rows = conn.execute(_PNL_SQL_WITH_TELE if attached else _PNL_SQL).fetchall()
    except sqlite3.Error:
        # If the attached schema doesn't have `runs.cost_usd` / `session_id`,
        # the WITH_TELE query throws. Retry without telemetry.
        rows = conn.execute(_PNL_SQL).fetchall()
    finally:
        if attached:
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
