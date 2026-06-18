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

## 5. Deferred to other layers (NOT built here, as of the Ledger task)

- Policy verdicts / routing thresholds (Policy layer — pure function). DONE — see
  `policy.evaluate_spend` (added in the Policy task).
- The `pre_tool_call` synchronous hold that reads `approval_requests`
  pending→approved/rejected/timeout (Enforcement). DONE — see `enforcement.py`.
- Capture writing `llm_cost`/revenue/external_spend facts and `link_session` calls.

## 6. Enforcement layer — §4 wording correction + flagged gaps (do NOT edit CLAUDE.md)

The Enforcement layer (`enforcement.py`, the `pre_tool_call` gate for real Stripe
spend) surfaced one doc correction and two gaps:

- **§4 "matches tool names" → should read "matches terminal commands".** GROUND
  TRUTH: the Stripe skills do NOT register their own tools — every command runs
  through the `terminal` tool, so the hook payload is
  `{tool_name:"terminal", tool_input:{command:"..."}}`. Enforcement matches
  `tool_name=="terminal"` AND a spend command pattern (`stripe projects add`,
  `stripe projects upgrade`, `mpp pay`), explicitly NOT `stripe_*` tool names. The
  existing `hook.py` (which matches `stripe_*` / `argus_request_spend`) is left in
  place as a defense-in-depth backstop; `__init__.register` wires both — their
  matchers don't overlap, so they never double-gate the same call. A later doc
  commit should reword §4.

- **GAP — request_spend declaration storage.** §9.1(c) projected_usd resolution
  correlates a terminal spend command to a prior `request_spend` declaration.
  Enforcement keeps that correlation in an **in-process cache**
  (`enforcement.declare_spend` / `_declarations`). Recording declarations durably
  is **Capture's** job; a cross-process implementation needs a small store table
  (e.g. `spend_declarations`) written by Capture and read by Enforcement. Flagged,
  not built — did NOT bolt it onto the Ledger store.

- **GAP — session→job→cost_center read getters.** To resolve `cost_center_id` from
  a session/job purely via the `jobs`/`job_sessions` bridge (when there is no
  declaration), the store would need read getters like `get_job_for_session(session_id)`
  / `get_cost_center_for_job(job_id)`. They don't exist; per the task's "flag, don't
  bolt onto the store" rule, Enforcement currently takes `cost_center_id` from the
  declaration and falls back to `default`. Add those getters to the store later if
  declaration-less attribution is needed.

- **Fail-closed invariant (the financial-gate guarantee).** Hermes fails OPEN on a
  hook exception (`tool_executor.py:283`). On a matched spend command, `enforcement`
  wraps the entire gate body in try/except and returns BLOCK on ANY error (db,
  snapshot, Policy, even if the error-audit write itself fails). There is no code
  path on a matched spend command that returns allow without an explicit Policy
  ALLOW or a human approval; timeout is treated as rejection.
