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

## 7. Capture layer — the two gaps closed + flagged deviations (do NOT edit CLAUDE.md)

The Capture layer (`capture.py` + the durable-declaration store change) closes the
two gaps §6 flagged and adds the confirmed-spend recorder. New modules/tests:
`capture.py`, `matchers.py` (shared), `tests/test_capture.py`,
`tests/test_declarations.py`, `tests/test_matchers.py`, `tests/test_config_cc.py`.

- **CHANGE 1 — shared matcher.** The spend-command patterns now live ONLY in
  `matchers.py`. `enforcement.py` re-exports them (`is_spend_command`,
  `_command_of`, `_SPEND_PATTERNS`) instead of defining its own copy; `capture.py`
  imports the same module. Behavior is identical — `tests/test_matchers.py` asserts
  the gate and the recorder expose the SAME function objects (a drift would fail).

- **CHANGE 2 — durable declarations.** Enforcement's in-process correlation cache
  is gone. Declarations are a durable, cross-process table `spend_declarations`
  (`schema.sql`), written by Capture's `request_spend` and read by the gate via the
  store's `find_open_declaration`. The store gained PLAIN CRUD (`insert_declaration`,
  `find_open_declaration`, `mark_declaration_consumed`) and the two missing chain
  getters (`get_job_for_session`, `get_cost_center_for_job`). Enforcement's only
  source change: `declare_spend` / `_lookup_declaration` were rewired to the store;
  the gate body is untouched. `cost_center` is no longer carried on the declaration
  — it is resolved via the jobs bridge (`get_cost_center_for_job`), so the
  declaration table matches the brief's columns exactly.

- **GROUND-TRUTH correction — post_tool_call payload.** The build brief said the
  correlation ids arrive under `extra.tool_call_id` / `extra.task_id`. The Hermes
  source (`model_tools._emit_post_tool_call_hook`, `agent/tool_executor.py`) passes
  them as TOP-LEVEL kwargs: `tool_name, args, result, task_id, session_id,
  tool_call_id, status, …` — no `extra` wrapper. Per CLAUDE.md §11 (the example
  wins) `capture.on_post_tool_call` reads the top-level kwargs. Success is
  `status == "ok"` (the Hermes-derived field; result-error envelope as fallback).
  `on_session_start` is NOT wired, so all linking is lazy inside the wired hooks.

- **FLAG — `ledger.tool_call_id` column (store extension beyond the named CRUD).**
  CHANGE 2's named schema work was `spend_declarations` only, but §C mandates "never
  write two external_spend rows for the same tool_call_id." Idempotency needs the
  dedup key PERSISTED on the money row, so a nullable `tool_call_id` column was added
  to `ledger` (fresh DBs via `schema.sql`; older DBs via the additive `ALTER` in
  `db._ensure_schema`, mirroring the existing `compute_allocations` migration) plus a
  passive guard `external_spend_recorded(tool_call_id)`. Every other writer leaves it
  NULL. Flagged here per the "don't silently bolt onto the passive store" rule — it
  is plain data access (a column + a SELECT), no policy.

- **A1 is honored — Capture never re-measures llm_cost.** Capture's only llm_cost
  job is keeping `job_sessions` links present (`ensure_attribution`) so the store's
  read-only telemetry ATTACH attributes cost to the right job at query time.
  `record_llm_cost_fallback` writes an explicit `llm_cost` row ONLY under the A2
  (no-telemetry) path.

- **Revenue (§9.2) — the one remaining P&L input, NOT built here.** Per §9.2 revenue
  enters via a `plugin_api.py` Stripe webhook / sim path, not the hook layer. P&L =
  revenue − llm_cost − external_spend; Capture now durably supplies external_spend
  (confirmed) and the attribution for llm_cost (derived). Revenue intake stays the
  open item for the dashboard/API task.

## 8. Revenue intake layer — three-sided P&L closed (do NOT edit CLAUDE.md)

Revenue now enters via `dashboard/plugin_api.py` (HTTP, §9.2) — completing
`revenue − llm_cost − external_spend` as a COMPUTED ledger value. New tests:
`tests/test_revenue.py`. Only remaining piece is the Dashboard UI.

- **Two intake paths.** `POST /revenue/sim` (demo-only, `source="stripe-sim"`,
  requires job_id → 400 otherwise) and `POST /revenue/stripe` (real webhook). Both
  idempotent on `ref` via the new passive guard `db.revenue_recorded(ref)` (mirrors
  `external_spend_recorded`). The pre-existing unsigned `/webhooks/stripe` (Phase 3)
  is left untouched for back-compat; `/revenue/stripe` is the signed successor.

- **Signature verification is mandatory and stdlib-only.** `_verify_stripe_signature`
  implements Stripe's scheme (`t=<ts>,v1=<hmac_sha256>`, signed payload `"{t}.{body}"`,
  constant-time compare, 300s replay window) with no `stripe` dependency. Invalid /
  missing signature, or no configured secret → **400, no row written** (fail closed).
  Secret from `config.stripe_webhook_secret()` (env `ARGUS_STRIPE_WEBHOOK_SECRET` /
  `STRIPE_WEBHOOK_SECRET`, test-mode per §10).

- **Attribution rule.** `metadata.job_id` present → revenue row for that job. ABSENT
  → recorded to the `unattributed` sentinel job (registered once to an `unattributed`
  cost center) + `revenue_unattributed` audit. Treasury stays correct; per-job P&L is
  never polluted by a guessed job. No NOT-NULL/FK awkwardness (register_job seeds the
  sentinel cost center first), so nothing to flag there.

- **Read endpoints added now.** `/pnl` switched to the canonical `pnl_by_job()` (A1
  ATTACH, rounded). `/treasury` returns the SOLVENT-style close
  `{seed_capital, gross_revenue, total_spend, net_pnl, cash_position}` — all computed.
  `seed_capital` from `config.seed_capital()` (env `ARGUS_SEED_CAPITAL` / yaml
  `seed_capital:` / 0.0).

- **RESOLVED — treasury and /pnl now share ONE llm_cost basis.** (Was: treasury
  summed llm_cost from the ledger while `/pnl` derived it from telemetry via the A1
  ATTACH, so under A1 — the §5 primary, where llm_cost is NOT a ledger row —
  cash_position counted $0 inference cost and OVER-stated profit.) The derivation is
  now a single helper `db._llm_cost_by_job()` (with `_total_llm_cost()` for the whole
  ledger): A1 = `SUM(telemetry.runs.cost_usd)` attributed via `job_sessions` when
  `telemetry.db` is present; A2 = `SUM(ledger llm_cost rows)` when it is absent —
  **never both** (A1 preferred; stray A2 rows are ignored under A1, so no
  double-count). `get_pnl_per_job` and the treasury close (`_total_llm_cost` →
  `money_totals` → `cash_position`, and the `/treasury` route) all call it, so /pnl
  and /treasury can never disagree on cost. `ledger_money_totals` was renamed to
  `money_totals` (its llm_cost is no longer ledger-only). Read-path refactor only —
  no schema change, no writes, no new business logic. Demo behavior unchanged (no
  telemetry.db in the demo → A2 → ledger basis, exactly as before). Tests:
  `tests/test_treasury_basis.py` (A1, A2, no-double-count, multi-job invariant).
  Side benefit: the old `_PNL_SQL_WITH_TELE` itself summed `ledger.llm_cost +
  telemetry` per job — a latent per-job double-count — which the single-basis helper
  also eliminates.
