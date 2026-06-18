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
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists


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
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO ledger(ts, job_id, kind, amount_usd, source, ref, session_id)"
        " VALUES(?,?,?,?,?,?,?)",
        (_now(), job_id, kind, _quantize(amount_usd), source, ref, session_id),
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


def pnl_by_job() -> List[Dict[str, Any]]:
    """Canonical P&L surface (CLAUDE.md store API). Same A1/A2 logic as
    ``get_pnl_per_job`` but with every money column rounded to cents on read
    to avoid float drift in the summed values."""
    out = []
    for r in get_pnl_per_job():
        d = dict(r)
        for k in ("revenue", "llm_cost", "external_spend", "pnl"):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 2)
        out.append(d)
    return out


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

# When telemetry is attached, fold its per-session cost into llm_cost. The
# correct attribution chain is telemetry.runs.session_id → job_sessions.session_id
# → jobs.job_id (NOT ledger.session_id — see MIGRATION_NOTES.md / CLAUDE.md §5 vs §8).
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
        SELECT js.job_id AS job_id,
               COALESCE(SUM(r.cost_usd), 0.0) AS llm_cost_tele
          FROM job_sessions js
          JOIN telemetry.runs r ON r.session_id = js.session_id
         GROUP BY js.job_id
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
