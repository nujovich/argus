# Migration notes — Ledger layer (attribution bridge + money handling)

Scope of this change: the **Ledger layer only** (CLAUDE.md §2 — passive store).
No Capture / Policy / Enforcement / Dashboard logic was added. The work extended
the existing single-source-of-truth module `db.py` in place rather than forking a
second ledger (which would violate §2 "Ledger is the center").

## 1. §5 ↔ §8 reconciliation — the attribution bridge (ACTION REQUIRED LATER)

**The inconsistency.** §5 specifies joining telemetry on `ledger.session_id`, but
§8's `ledger` table has no reliable session→cost_center chain, and the documented
attribution chain is `session → job → cost_center`. The ledger carries `job_id`;
budgets carry `cost_center_id`; there was **no bridge** between a telemetry
`session_id` and a `job_id`, nor between a `job_id` and its `cost_center_id`.

**What was added (minimal bridge):**

```sql
CREATE TABLE cost_centers (id TEXT PRIMARY KEY, label TEXT, created_at TEXT NOT NULL);
CREATE TABLE budgets (cost_center_id TEXT PRIMARY KEY REFERENCES cost_centers(id),
                      limit_usd REAL NOT NULL, soft_threshold REAL, ...);
CREATE TABLE jobs (job_id TEXT PRIMARY KEY,
                   cost_center_id TEXT NOT NULL REFERENCES cost_centers(id),
                   created_at TEXT NOT NULL);
CREATE TABLE job_sessions (session_id TEXT PRIMARY KEY,        -- telemetry.runs.session_id / task_id
                           job_id TEXT NOT NULL REFERENCES jobs(job_id));
```

The §8 tables (`ledger`, `approval_requests`, `audit_trail`, `auth_tokens`,
`compute_allocations`) are unchanged. `ledger.session_id` is kept for backward
compatibility but is **no longer the P&L bridge**.

**The correct A1 join is now:**
`telemetry.runs.session_id → job_sessions.session_id → jobs.job_id`
(implemented in `db._PNL_SQL_WITH_TELE`). Per-cost-center rollups derive spend via
`jobs.cost_center_id` (`db.ledger_snapshot`).

> **TODO for a later doc-only commit (not this session):** update CLAUDE.md §5's
> join clause to reference `job_sessions` instead of `ledger.session_id`. Do NOT
> edit CLAUDE.md in the Ledger implementation session.

## 2. Two further doc inconsistencies found (record only; do not edit CLAUDE.md now)

- **§8 has no `cost_centers` / `budgets` tables.** The build brief listed them as
  part of "§8 verbatim", but actual §8 defines neither — cost centers + budgets
  historically lived in `cost_centers.yaml` + `config.py` (`Budget` dataclass).
  They are now first-class tables (seeded from YAML via `db.seed_from_config`) so
  the FK from `jobs.cost_center_id` and budget rollups have something to reference.
  `config.py` still works and is untouched. A later doc commit should add these to §8.
- **There is no §9.3.** CLAUDE.md §9 ("Design decisions") has items 1–7 and no
  subsections, and `cost_centers.yaml` shipped with no job→cost_center map. The
  brief's "§9.3 cost_centers.yaml job→cost_center mapping" was implemented anyway:
  `seed_from_config` reads an optional top-level `jobs:` map (see
  `cost_centers.sample.yaml`). A later doc commit should formalize this as §9.3.

## 3. Money representation (FLAG for post-hackathon)

Per §8 the `amount_usd` column stays **REAL**. To avoid float drift across many
small writes, every amount is **quantized to whole cents (2 dp, ROUND_HALF_UP)**
on write (`db._quantize`, applied in `insert_ledger_row` → `append_fact` and in
budget upserts), and all P&L / snapshot sums are **rounded to 2 dp on read**
(`pnl_by_job`, `ledger_snapshot`, `get_cost_center_spent`).

> **FLAG:** storing money as **integer cents** (INTEGER column) would be strictly
> more correct than REAL-plus-quantization and is the recommended change if money
> handling is revisited post-hackathon. The column type was **not** changed now to
> avoid a schema migration of live data and to keep §8 verbatim.

## 4. Compatibility

- All existing `db.py` callers (hook.py, policy.py, dashboard/plugin_api.py + tests)
  are unchanged. `insert_ledger_row`, `create_approval_request`, `get_pnl_per_job`,
  `get_cost_center_spent`, etc. keep working.
- `decide_approval` was generalized to accept both the canonical
  `decide_approval(id, status, decided_by, reason)` (now incl. `timeout`) and the
  legacy `decide_approval(id, decision=..., actor=...)` keyword form. It remains
  idempotent and WAL-safe (only a `pending` row transitions).
- Schema is now canonicalized in `schema.sql`, loaded idempotently by
  `db._ensure_schema` on every connection (WAL + foreign_keys ON), followed by the
  additive `ALTER`s for older DBs.

## 5. Deferred to other layers (NOT built here)

- Policy verdicts / routing thresholds (Policy layer — pure function).
- The `pre_tool_call` synchronous hold that reads `approval_requests`
  pending→approved/rejected/timeout (Enforcement; mechanism validated separately).
- Capture writing `llm_cost`/revenue/external_spend facts and `link_session` calls.
