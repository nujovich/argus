-- Argus ledger schema — the single source of truth (CLAUDE.md §2, §8).
--
-- Canonical, idempotent (every statement is IF NOT EXISTS). db._ensure_schema()
-- loads and executes this file on every connection, then applies the additive
-- ALTERs that older DBs may be missing. This is a PASSIVE store: no policy, no
-- side effects — just tables the other layers read from and write to.
--
-- §8 schema is reproduced verbatim. The `cost_centers`, `budgets`, `jobs`, and
-- `job_sessions` tables are the attribution bridge added so the A1 P&L join and
-- per-cost-center budget rollups actually work (see MIGRATION_NOTES.md).

-- ── §8: the dollar ledger ──────────────────────────────────────────────────
-- NOTE (Capture extension, flagged): `tool_call_id` is NOT in §8's verbatim
-- schema. Capture's post_tool_call recorder needs an idempotency key so a
-- replayed confirmation never double-writes a confirmed spend (see capture.py /
-- MIGRATION_NOTES.md). It is nullable: every other writer (revenue, llm_cost,
-- the declaration-path external_spend in hook.py) leaves it NULL.
CREATE TABLE IF NOT EXISTS ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    job_id       TEXT    NOT NULL,
    kind         TEXT    NOT NULL CHECK (kind IN
                   ('revenue', 'llm_cost', 'external_spend')),
    amount_usd   REAL    NOT NULL,
    source       TEXT,
    ref          TEXT,
    session_id   TEXT,
    tool_call_id TEXT
);
CREATE INDEX IF NOT EXISTS ledger_job_idx ON ledger(job_id);
CREATE INDEX IF NOT EXISTS ledger_session_idx ON ledger(session_id);
-- NOTE: the index on tool_call_id is created in db._ensure_schema AFTER the
-- additive ALTER, because on an older DB the `ledger` table predates the column
-- (CREATE TABLE IF NOT EXISTS is skipped) and indexing a missing column here
-- would abort the whole script before spend_declarations is created.

-- ── Capture: durable spend declarations (CLAUDE.md §2 Capture, §9.1(c)) ──────
-- The intent half of the intent→confirmation loop. The agent declares a spend
-- BEFORE the terminal command runs; Enforcement reads the open declaration to
-- gate, and Capture's post_tool_call recorder consumes it once the REAL spend
-- is confirmed. Durable (survives process restart) and cross-process — it
-- replaces Enforcement's old in-process correlation cache. PASSIVE: no policy,
-- no cost_center column (cc is resolved via the jobs bridge — §8 attribution
-- chain — not carried on the declaration).
CREATE TABLE IF NOT EXISTS spend_declarations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT    NOT NULL,
    session_id    TEXT,
    projected_usd REAL    NOT NULL,
    ref           TEXT,
    declared_at   TEXT    NOT NULL,
    consumed_at   TEXT
);
CREATE INDEX IF NOT EXISTS spend_decl_session_idx ON spend_declarations(session_id);
CREATE INDEX IF NOT EXISTS spend_decl_job_idx ON spend_declarations(job_id);
CREATE INDEX IF NOT EXISTS spend_decl_open_idx ON spend_declarations(consumed_at);

-- ── Attribution bridge — session → job → cost_center (CLAUDE.md §8 line 409) ─
-- §5 says to join telemetry on a session→job→cost_center chain, but §8's ledger
-- has no session_id bridge and budgets carry cost_center_id. These two tables are
-- that missing bridge. See MIGRATION_NOTES.md for the §5 join-clause reconciliation.
CREATE TABLE IF NOT EXISTS cost_centers (
    id          TEXT PRIMARY KEY,
    label       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    cost_center_id          TEXT PRIMARY KEY REFERENCES cost_centers(id),
    limit_usd               REAL NOT NULL,
    soft_threshold          REAL NOT NULL DEFAULT 0.8,
    auto_approve_under_usd  REAL NOT NULL DEFAULT 0.0,
    manager_under_usd       REAL,
    -- Phase 4.5 compute-tier policy fields (optional; cash-only when unset).
    ultra_model             TEXT,
    base_model              TEXT,
    ultra_min_revenue_usd   REAL,
    ultra_min_margin_usd    REAL,
    reject_below_margin_usd REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    cost_center_id  TEXT NOT NULL REFERENCES cost_centers(id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_cc_idx ON jobs(cost_center_id);

CREATE TABLE IF NOT EXISTS job_sessions (          -- one job → many Hermes sessions
    session_id  TEXT PRIMARY KEY,                  -- telemetry.runs.session_id / task_id
    job_id      TEXT NOT NULL REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS job_sessions_job_idx ON job_sessions(job_id);

-- ── §8: approvals ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS approval_requests (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    cost_center_id  TEXT NOT NULL,
    projected_usd   REAL NOT NULL,
    level           TEXT NOT NULL,              -- 'manager' | 'finance' | 'unspecified'
    status          TEXT NOT NULL CHECK (status IN
                      ('pending','approved','rejected','timeout')),
    decided_at      TEXT,
    decided_by      TEXT,
    reason          TEXT,
    tool_name       TEXT,
    ref             TEXT
);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approval_requests(status);

-- ── §8: audit trail ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_trail (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    event   TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit_trail(ts DESC);

-- ── §8: auth tokens (defense in depth, §6) ─────────────────────────────────
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

-- ── Phase 4.5: compute allocator ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compute_allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    cost_center_id  TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('ultra','base','reject')),
    model           TEXT NOT NULL,
    compute_budget_usd   REAL NOT NULL,
    expected_revenue_usd REAL,
    expected_margin_usd  REAL,
    status          TEXT NOT NULL CHECK (status IN
                      ('active','downgraded','killed','done')),
    downgrade_reason TEXT,
    session_id      TEXT,
    auth_token      TEXT
);
CREATE INDEX IF NOT EXISTS compute_alloc_job_idx ON compute_allocations(job_id);
CREATE INDEX IF NOT EXISTS compute_alloc_status_idx ON compute_allocations(status);

-- For future migrations.
CREATE TABLE IF NOT EXISTS _argus_schema_version (
    version INTEGER PRIMARY KEY
);
