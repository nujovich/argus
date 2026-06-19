"""Argus ledger — SQLite WAL DB.

See CLAUDE.md §8 for the schema and §5 for the read-only ATTACH to
hermes-telemetry. This module is the only writer; Policy is pure and
reads via snapshot helpers below.
"""

from __future__ import annotations

import functools
import json
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, TypeVar

import config as _cfg  # plugin dir is on sys.path at runtime


_local = threading.local()
_schema_lock = threading.Lock()

# WAL mode and the schema are persistent, file-level state shared by every
# connection to a DB. We apply them exactly ONCE per DB path, guarded by this
# lock. Re-running ``PRAGMA journal_mode=WAL`` on every new (per-thread)
# connection is the root cause of the "database is locked" flake: the WAL-mode
# switch needs a brief exclusive lock and — unlike normal statements — does NOT
# honor ``busy_timeout``, so it raises SQLITE_BUSY immediately whenever another
# thread's connection is mid-write. Once the file is in WAL mode, new
# connections simply observe it; they never need to re-set it.
_init_lock = threading.Lock()
_initialized_paths: set = set()

# Write-retry tuning (see _retry_write).
_WRITE_RETRIES = 5
_BACKOFF_MIN_S = 0.025
_BACKOFF_MAX_S = 0.150


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


def _get_db_path() -> Path:
    path = _cfg.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _is_busy_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _get_conn() -> sqlite3.Connection:
    """Return this thread's connection, creating it if needed.

    Connections are strictly per-thread (thread-local). SQLite connections are
    not safe to share across threads, and the §6 approval poll runs on a
    different thread than the tool worker and the dashboard reader — each gets
    its own connection. We pass ``check_same_thread=False`` only so that
    test/teardown helpers may close a connection from another thread; the
    connection is never *used* for queries off its owning thread.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    path = _get_db_path()
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False, uri=True
    )
    conn.row_factory = sqlite3.Row
    # Per-connection pragmas — cheap, take no contended lock:
    #  - busy_timeout: wait up to 5s and let SQLite retry internally instead of
    #    raising SQLITE_BUSY (covers the read poll path and write contention).
    #  - synchronous=NORMAL: safe + faster under WAL.
    #  - foreign_keys: enforce the bridge FKs.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _init_db_file(conn, str(path))
    _local.conn = conn
    return conn


def _init_db_file(conn: sqlite3.Connection, path_key: str) -> None:
    """Apply WAL mode + schema exactly once per DB file (process-wide)."""
    if path_key in _initialized_paths:
        return
    with _init_lock:
        if path_key in _initialized_paths:
            return
        # WAL switch + DDL both take a brief write lock; do them under retry so
        # a concurrent first-touch from another process can't make startup throw.
        _retry_busy(lambda: conn.execute("PRAGMA journal_mode = WAL"))
        with _schema_lock:
            _retry_busy(lambda: _ensure_schema(conn))
        _initialized_paths.add(path_key)


def reset_connection_for_tests() -> None:
    """Drop the per-thread connection so the next call reopens a fresh DB.

    Also clears the per-path init guard so a fresh (new tmpdir) DB re-applies
    WAL + schema. Test-only hook.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
    with _init_lock:
        _initialized_paths.clear()


_T = TypeVar("_T")


def _retry_busy(fn: Callable[[], _T]) -> _T:
    """Run ``fn``; on a transient 'database is locked/busy' OperationalError,
    retry up to _WRITE_RETRIES times with jittered backoff, then re-raise.

    Reads under WAL don't block writers, so this wraps WRITE paths (and the
    one-time WAL/DDL init). The read poll path relies on busy_timeout alone.
    """
    last: Optional[sqlite3.OperationalError] = None
    for attempt in range(_WRITE_RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc):
                raise
            last = exc
            time.sleep(random.uniform(_BACKOFF_MIN_S, _BACKOFF_MAX_S))
    assert last is not None
    raise last


def _retry_write(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Decorator: wrap a write function so transient WAL lock/busy errors are
    retried with backoff instead of escaping to the caller."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        return _retry_busy(lambda: fn(*args, **kwargs))

    return wrapper


_SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "schema.sql"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # Canonical schema lives in schema.sql (single source of truth, all
    # statements idempotent). Apply it, then run additive ALTERs that older
    # DBs may be missing.
    conn.executescript(_SCHEMA_SQL_PATH.read_text())
    # Idempotent schema migrations — add columns that may not exist on
    # an older DB without breaking startup.
    for col_sql in (
        "ALTER TABLE compute_allocations ADD COLUMN actual_model TEXT",
        "ALTER TABLE compute_allocations ADD COLUMN integrity_status TEXT",
        # Capture idempotency key — older DBs created before the column existed.
        "ALTER TABLE ledger ADD COLUMN tool_call_id TEXT",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    # Index on tool_call_id is created here (not in schema.sql) so it runs only
    # after the column is guaranteed to exist on both fresh and older DBs.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ledger_tool_call_idx ON ledger(tool_call_id)"
        )
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_CENT = Decimal("0.01")
LEDGER_KINDS = frozenset({"revenue", "llm_cost", "external_spend"})


def _quantize(amount_usd: float) -> float:
    """Quantize a dollar amount to whole cents (2 decimals) to avoid float
    drift across many small writes. The column stays REAL per §8; integer
    cents would be stricter — see MIGRATION_NOTES.md."""
    return float(Decimal(str(amount_usd)).quantize(_CENT, rounding=ROUND_HALF_UP))


@_retry_write
def insert_ledger_row(
    *,
    job_id: str,
    kind: str,
    amount_usd: float,
    source: Optional[str] = None,
    ref: Optional[str] = None,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO ledger(ts, job_id, kind, amount_usd, source, ref, session_id,"
        " tool_call_id) VALUES(?,?,?,?,?,?,?,?)",
        (_now(), job_id, kind, _quantize(amount_usd), source, ref, session_id,
         tool_call_id),
    )
    return int(cur.lastrowid)


@_retry_write
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


@_retry_write
def decide_approval(
    req_id: str,
    status: Optional[str] = None,
    decided_by: Optional[str] = None,
    reason: Optional[str] = None,
    *,
    decision: Optional[str] = None,
    actor: Optional[str] = None,
) -> bool:
    """Transition a *pending* approval to a terminal state. Idempotent and
    WAL-safe: the UPDATE is guarded by ``status='pending'`` so only the first
    decision wins and concurrent readers never see a half-applied row. A second
    decide on an already-decided row is a no-op (returns False).

    Accepts both the canonical Ledger signature
    ``decide_approval(id, status, decided_by, reason)`` with
    ``status ∈ {approved, rejected, timeout}`` and the legacy keyword form
    ``decide_approval(id, decision=..., actor=...)`` used by existing callers.
    """
    final_status = status if status is not None else decision
    final_actor = decided_by if decided_by is not None else actor
    if final_status not in {"approved", "rejected", "timeout"}:
        raise ValueError(f"invalid status: {final_status!r}")
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE approval_requests SET status=?, decided_at=?, decided_by=?, reason=?"
        " WHERE id=? AND status='pending'",
        (final_status, _now(), final_actor, reason, req_id),
    )
    return cur.rowcount > 0


@_retry_write
def mark_timeout(req_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE approval_requests SET status='timeout', decided_at=?, reason='timeout'"
        " WHERE id=? AND status='pending'",
        (_now(), req_id),
    )
    return cur.rowcount > 0


@_retry_write
def log_audit(actor: str, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO audit_trail(ts, actor, event, payload) VALUES(?,?,?,?)",
        (_now(), actor, event, json.dumps(payload) if payload is not None else None),
    )


# ---------------------------------------------------------------------------
# Store API — the passive data-access surface other layers call (CLAUDE.md §2).
# Pure data ops: no policy verdicts, no blocking, no Stripe. Callers decide.
# Clean names mirror the Ledger interface contract; thin wrappers over the
# writers above where they already exist.
# ---------------------------------------------------------------------------


def init_db() -> sqlite3.Connection:
    """Open (or reuse) the connection and apply the schema idempotently.
    Returns the live connection. WAL is enabled in ``_get_conn``."""
    return _get_conn()


def migrate() -> None:
    """Re-apply the idempotent schema to the current connection."""
    conn = _get_conn()
    with _schema_lock:
        _retry_busy(lambda: _ensure_schema(conn))


def append_audit(actor: str, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Canonical name for an audit write (see CLAUDE.md store API)."""
    log_audit(actor, event, payload)


# ── Attribution bridge writers — session → job → cost_center ────────────────


@_retry_write
def upsert_cost_center(cost_center_id: str, label: Optional[str] = None) -> None:
    """Idempotently ensure a cost_centers row exists."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO cost_centers(id, label, created_at) VALUES(?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET label=COALESCE(excluded.label, cost_centers.label)",
        (cost_center_id, label, _now()),
    )


@_retry_write
def upsert_budget(
    cost_center_id: str,
    *,
    limit_usd: float,
    soft_threshold: float = 0.8,
    auto_approve_under_usd: float = 0.0,
    manager_under_usd: Optional[float] = None,
    ultra_model: Optional[str] = None,
    base_model: Optional[str] = None,
    ultra_min_revenue_usd: Optional[float] = None,
    ultra_min_margin_usd: Optional[float] = None,
    reject_below_margin_usd: Optional[float] = None,
) -> None:
    """Idempotently upsert a cost center's budget row. Requires the
    cost_centers row to exist (caller seeds it first / seed_from_config does)."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO budgets(cost_center_id, limit_usd, soft_threshold,"
        " auto_approve_under_usd, manager_under_usd, ultra_model, base_model,"
        " ultra_min_revenue_usd, ultra_min_margin_usd, reject_below_margin_usd)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cost_center_id) DO UPDATE SET"
        "   limit_usd=excluded.limit_usd,"
        "   soft_threshold=excluded.soft_threshold,"
        "   auto_approve_under_usd=excluded.auto_approve_under_usd,"
        "   manager_under_usd=excluded.manager_under_usd,"
        "   ultra_model=excluded.ultra_model,"
        "   base_model=excluded.base_model,"
        "   ultra_min_revenue_usd=excluded.ultra_min_revenue_usd,"
        "   ultra_min_margin_usd=excluded.ultra_min_margin_usd,"
        "   reject_below_margin_usd=excluded.reject_below_margin_usd",
        (
            cost_center_id, _quantize(limit_usd), float(soft_threshold),
            _quantize(auto_approve_under_usd),
            _quantize(manager_under_usd) if manager_under_usd is not None else None,
            ultra_model, base_model,
            _quantize(ultra_min_revenue_usd) if ultra_min_revenue_usd is not None else None,
            _quantize(ultra_min_margin_usd) if ultra_min_margin_usd is not None else None,
            _quantize(reject_below_margin_usd) if reject_below_margin_usd is not None else None,
        ),
    )


@_retry_write
def register_job(job_id: str, cost_center_id: str) -> None:
    """Map a job to a cost center (idempotent). Ensures the cost_centers row
    exists so the FK holds even if seed_from_config hasn't run."""
    upsert_cost_center(cost_center_id)
    conn = _get_conn()
    conn.execute(
        "INSERT INTO jobs(job_id, cost_center_id, created_at) VALUES(?,?,?)"
        " ON CONFLICT(job_id) DO UPDATE SET cost_center_id=excluded.cost_center_id",
        (job_id, cost_center_id, _now()),
    )


@_retry_write
def link_session(session_id: str, job_id: str) -> None:
    """Attach a Hermes session (telemetry.runs.session_id / task_id) to a job
    (idempotent). This is the bridge the A1 P&L join uses."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO job_sessions(session_id, job_id) VALUES(?,?)"
        " ON CONFLICT(session_id) DO UPDATE SET job_id=excluded.job_id",
        (session_id, job_id),
    )


# ── Durable spend declarations — Capture writes, Enforcement reads (§9.1(c)) ─
# Passive CRUD only: insert a row, find the open one, mark it consumed. No
# policy, no cost_center (cc is resolved via get_cost_center_for_job). This is
# the durable replacement for Enforcement's old in-process correlation cache.


@_retry_write
def insert_declaration(
    *,
    job_id: str,
    projected_usd: float,
    session_id: Optional[str] = None,
    ref: Optional[str] = None,
) -> int:
    """Record a spend declaration (the intent half of intent→confirmation)."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO spend_declarations(job_id, session_id, projected_usd, ref,"
        " declared_at) VALUES(?,?,?,?,?)",
        (str(job_id), session_id, _quantize(projected_usd), ref, _now()),
    )
    return int(cur.lastrowid)


def find_open_declaration(
    *, job_id: Optional[str] = None, session_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return the newest still-open (unconsumed) declaration matching
    ``session_id`` (preferred) or ``job_id``. Plain read; None if none open."""
    conn = _get_conn()
    if session_id:
        row = conn.execute(
            "SELECT * FROM spend_declarations"
            " WHERE session_id=? AND consumed_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
    if job_id:
        row = conn.execute(
            "SELECT * FROM spend_declarations"
            " WHERE job_id=? AND consumed_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
    return None


@_retry_write
def mark_declaration_consumed(decl_id: int) -> bool:
    """Atomically mark a declaration consumed. Guarded by ``consumed_at IS NULL``
    so only the first caller wins (idempotent); returns True iff this call
    consumed it."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE spend_declarations SET consumed_at=?"
        " WHERE id=? AND consumed_at IS NULL",
        (_now(), int(decl_id)),
    )
    return cur.rowcount > 0


# ── Attribution chain read getters (session → job → cost_center) ─────────────


def get_job_for_session(session_id: str) -> Optional[str]:
    """job_id linked to a Hermes session (via job_sessions), or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT job_id FROM job_sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return row["job_id"] if row else None


def get_cost_center_for_job(job_id: str) -> Optional[str]:
    """cost_center_id a job is mapped to (via jobs), or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT cost_center_id FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    return row["cost_center_id"] if row else None


def revenue_recorded(ref: str) -> bool:
    """True if a revenue ledger row already exists for this ref (the Stripe
    event/payment id, or the sim ref). Capture's revenue dedup guard — mirrors
    external_spend_recorded so a duplicate webhook delivery can't double-count.
    Plain read; no business logic."""
    if not ref:
        return False
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM ledger WHERE kind='revenue' AND ref=? LIMIT 1",
        (ref,),
    ).fetchone()
    return row is not None


def money_totals() -> Dict[str, float]:
    """Money totals for the treasury close (rounded to cents). ``revenue`` and
    ``external_spend`` are summed from the ledger; ``llm_cost`` uses the SAME
    single basis as ``pnl_by_job`` via ``_total_llm_cost()`` (A1 telemetry when
    present, else A2 ledger rows — never both; helper lives in the P&L section).

    This is why /pnl and /treasury can never disagree on inference cost: they
    derive llm_cost from one place. (Renamed from the old ``ledger_money_totals``
    whose llm_cost was ledger-only and under-counted A1 inference cost.)"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE kind WHEN 'revenue'        THEN amount_usd END), 0.0) AS revenue,"
        " COALESCE(SUM(CASE kind WHEN 'external_spend' THEN amount_usd END), 0.0) AS external_spend"
        " FROM ledger"
    ).fetchone()
    return {
        "revenue": round(float(row["revenue"] or 0.0), 2),
        "llm_cost": _total_llm_cost(),
        "external_spend": round(float(row["external_spend"] or 0.0), 2),
    }


def cash_position() -> float:
    """Treasury cash: seed_capital + Σrevenue − llm_cost − Σexternal_spend,
    rounded to cents (§9.2 close). llm_cost uses the SAME A1/A2 basis as
    pnl_by_job (via money_totals → _total_llm_cost), NOT a ledger-only sum — so
    treasury counts A1 inference cost instead of over-stating profit."""
    t = money_totals()
    return round(
        _cfg.seed_capital() + t["revenue"] - t["llm_cost"] - t["external_spend"], 2
    )


def external_spend_recorded(tool_call_id: str) -> bool:
    """True if an external_spend ledger row already exists for this
    tool_call_id. Capture's idempotency guard — a replayed post_tool_call must
    never double-write a confirmed spend."""
    if not tool_call_id:
        return False
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM ledger WHERE kind='external_spend' AND tool_call_id=? LIMIT 1",
        (tool_call_id,),
    ).fetchone()
    return row is not None


def seed_from_config(path: Optional[str] = None) -> Dict[str, int]:
    """Load cost_centers + budgets + (optional) job→cost_center map from a
    cost_centers.yaml (CLAUDE.md §9.3) and idempotently upsert them. Returns a
    small count summary. Passive: it only writes config-derived rows."""
    import yaml  # local import; config also depends on it

    cfg_path = Path(path) if path else _cfg.cost_centers_yaml_path()
    if not cfg_path.exists():
        return {"cost_centers": 0, "budgets": 0, "jobs": 0}
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    n_cc = n_bud = n_jobs = 0
    for cc_id, c in (raw.get("cost_centers") or {}).items():
        upsert_cost_center(cc_id, c.get("label", cc_id))
        n_cc += 1
        if "limit_usd" in c:
            upsert_budget(
                cc_id,
                limit_usd=float(c["limit_usd"]),
                soft_threshold=float(c.get("soft_threshold", 0.8)),
                auto_approve_under_usd=float(c.get("auto_approve_under_usd", 0.0)),
                manager_under_usd=(
                    float(c["manager_under_usd"])
                    if c.get("manager_under_usd") is not None else None
                ),
                ultra_model=c.get("ultra_model"),
                base_model=c.get("base_model"),
                ultra_min_revenue_usd=(
                    float(c["ultra_min_revenue_usd"])
                    if c.get("ultra_min_revenue_usd") is not None else None
                ),
                ultra_min_margin_usd=(
                    float(c["ultra_min_margin_usd"])
                    if c.get("ultra_min_margin_usd") is not None else None
                ),
                reject_below_margin_usd=(
                    float(c["reject_below_margin_usd"])
                    if c.get("reject_below_margin_usd") is not None else None
                ),
            )
            n_bud += 1
    # Optional job→cost_center map (CLAUDE.md §9.3). Accepts either
    #   jobs: {job_id: cost_center_id}  or  jobs: {job_id: {cost_center_id: ...}}
    for job_id, jv in (raw.get("jobs") or {}).items():
        cc = jv if isinstance(jv, str) else (jv or {}).get("cost_center_id")
        if cc:
            register_job(job_id, cc)
            n_jobs += 1
    return {"cost_centers": n_cc, "budgets": n_bud, "jobs": n_jobs}


def append_fact(
    job_id: str,
    kind: str,
    amount_usd: float,
    source: Optional[str] = None,
    ref: Optional[str] = None,
) -> int:
    """Append one ledger fact. ``kind ∈ {revenue, llm_cost, external_spend}``.
    Amount is quantized to cents on write. (``llm_cost`` rows are only used
    under the A2 / no-telemetry path; under A1 LLM cost comes from telemetry.)"""
    if kind not in LEDGER_KINDS:
        raise ValueError(f"invalid kind: {kind!r} (expected one of {sorted(LEDGER_KINDS)})")
    return insert_ledger_row(
        job_id=job_id, kind=kind, amount_usd=amount_usd, source=source, ref=ref
    )


def create_approval(job_id: str, cost_center_id: str, projected_usd: float,
                    level: str = "unspecified") -> str:
    """Create a pending approval and return its id. Routing/level is Policy's
    call; the Ledger only stores it (defaults to 'unspecified')."""
    return create_approval_request(
        job_id=job_id, cost_center_id=cost_center_id,
        projected_usd=projected_usd, level=level,
    )


def read_approval(req_id: str) -> Optional[Dict[str, Any]]:
    """Full approval row (or None)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM approval_requests WHERE id=?", (req_id,)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Auth tokens — defense in depth, see CLAUDE.md §6
# ---------------------------------------------------------------------------


def _now_epoch() -> float:
    return time.time()


@_retry_write
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


@_retry_write
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
    return round(float(row["s"] or 0.0), 2)


def budget_for(cost_center_id: str) -> Optional[Dict[str, Any]]:
    """The budget row for a cost center (or None). Read-only; Policy is pure
    and must not touch I/O, so it reads through here."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM budgets WHERE cost_center_id=?", (cost_center_id,)
    ).fetchone()
    return dict(row) if row else None


def ledger_snapshot(cost_center_id: str) -> Dict[str, Any]:
    """Current spend used vs budget for a cost center, derived via
    jobs.cost_center_id (the bridge — no caller-supplied job list needed).

    Spend = SUM(llm_cost + external_spend) over every job mapped to the
    center. Returns a passive snapshot dict; Policy turns it into a verdict.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(l.amount_usd), 0.0) AS s"
        "  FROM ledger l JOIN jobs j ON j.job_id = l.job_id"
        " WHERE j.cost_center_id = ? AND l.kind IN ('llm_cost','external_spend')",
        (cost_center_id,),
    ).fetchone()
    spent = round(float(row["s"] or 0.0), 2)
    budget = budget_for(cost_center_id)
    limit_usd = float(budget["limit_usd"]) if budget else None
    return {
        "cost_center_id": cost_center_id,
        "spent_usd": spent,
        "limit_usd": limit_usd,
        "remaining_usd": (round(limit_usd - spent, 2) if limit_usd is not None else None),
        "soft_threshold": (float(budget["soft_threshold"]) if budget else None),
    }


# ---------------------------------------------------------------------------
# P&L — llm_cost from hermes-telemetry via read-only ATTACH (A1, §5 primary),
# with the A2 ledger-row fallback. ONE basis decision, shared by /pnl AND the
# treasury close so the two can never diverge (MIGRATION_NOTES §8 fix).
# ---------------------------------------------------------------------------


def _llm_cost_by_job() -> Dict[str, float]:
    """Per-job llm_cost under a SINGLE basis — never both (CLAUDE.md §5):

      A1 (telemetry.db present): SUM(telemetry.runs.cost_usd) attributed via
          job_sessions (session → job). Ledger ``llm_cost`` rows are IGNORED in
          this mode, so a deployment that has BOTH never double-counts.
      A2 (telemetry.db absent / unreadable): SUM(ledger ``llm_cost`` rows).

    This is the one place llm_cost is derived. ``get_pnl_per_job`` and the
    treasury close (``_total_llm_cost`` → ``money_totals`` → ``cash_position``)
    both call it, so /pnl and /treasury always agree on inference cost."""
    conn = _get_conn()
    tele_path = _cfg.telemetry_db_path()
    if tele_path.exists():
        attached = False
        try:
            conn.execute(
                "ATTACH DATABASE ? AS telemetry", (f"file:{tele_path}?mode=ro",)
            )
            attached = True
            rows = conn.execute(
                "SELECT js.job_id AS job_id, COALESCE(SUM(r.cost_usd), 0.0) AS c"
                "  FROM job_sessions js"
                "  JOIN telemetry.runs r ON r.session_id = js.session_id"
                " GROUP BY js.job_id"
            ).fetchall()
            return {r["job_id"]: round(float(r["c"] or 0.0), 2) for r in rows}
        except sqlite3.Error:
            # Telemetry present but unreadable (locked / wrong schema / no URI
            # support) → fall through to the A2 ledger basis. Never raise.
            pass
        finally:
            if attached:
                try:
                    conn.execute("DETACH DATABASE telemetry")
                except sqlite3.OperationalError:
                    pass
    # A2 fallback — llm_cost is a real ledger row.
    rows = conn.execute(
        "SELECT job_id, COALESCE(SUM(amount_usd), 0.0) AS c FROM ledger"
        " WHERE kind='llm_cost' GROUP BY job_id"
    ).fetchall()
    return {r["job_id"]: round(float(r["c"] or 0.0), 2) for r in rows}


def _total_llm_cost() -> float:
    """Whole-ledger llm_cost under the single A1/A2 basis (sum of
    ``_llm_cost_by_job``). The treasury close uses this so cash_position counts
    A1 inference cost — never the ledger-only sum that over-stated profit."""
    return round(sum(_llm_cost_by_job().values()), 2)


def get_pnl_per_job() -> List[Dict[str, Any]]:
    """P&L rolled up per job_id: revenue − llm_cost − external_spend.

    ``revenue`` / ``external_spend`` come from Argus's ledger; ``llm_cost`` comes
    from ``_llm_cost_by_job`` (A1 telemetry via job_sessions when present, else
    the A2 ledger basis — one basis, never summed). The row set is every job
    with a ledger row (a job that exists only as orphan telemetry cost is not
    surfaced — unchanged from the prior ATTACH query)."""
    conn = _get_conn()
    led = conn.execute(
        "SELECT job_id,"
        " SUM(CASE kind WHEN 'revenue'        THEN amount_usd ELSE 0 END) AS revenue,"
        " SUM(CASE kind WHEN 'external_spend' THEN amount_usd ELSE 0 END) AS external_spend"
        " FROM ledger GROUP BY job_id"
    ).fetchall()
    llm_by_job = _llm_cost_by_job()
    rows: List[Dict[str, Any]] = []
    for r in led:
        jid = r["job_id"]
        revenue = float(r["revenue"] or 0.0)
        external = float(r["external_spend"] or 0.0)
        llm = float(llm_by_job.get(jid, 0.0))
        rows.append({
            "job_id": jid,
            "revenue": revenue,
            "llm_cost": llm,
            "external_spend": external,
            "pnl": revenue - llm - external,
        })
    rows.sort(key=lambda d: d["job_id"])
    return rows


def pnl_by_job() -> List[Dict[str, Any]]:
    """Canonical P&L surface (CLAUDE.md store API). Same single-basis logic as
    ``get_pnl_per_job`` with every money column rounded to cents on read to
    avoid float drift in the summed values."""
    out = []
    for r in get_pnl_per_job():
        d = dict(r)
        for k in ("revenue", "llm_cost", "external_spend", "pnl"):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 2)
        out.append(d)
    return out
