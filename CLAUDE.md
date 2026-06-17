# Argus — design doc (CLAUDE.md)

> Single source of truth for design decisions in this repo. All future work
> must respect this document; deviations require an explicit update here.
>
> Status: **Phase 1 — design only.** No business logic implemented yet.
> Phase 2 (skeleton) is pending review of this document.

Name story: **Argus Panoptes**, the hundred-eyed guardian of Greek myth — a
hundred eyes on every dollar an autonomous agent spends.

Tagline: *Stripe gives agents a wallet; Argus puts a hundred eyes on it.*

---

## 1. Product thesis

Argus is a **horizontal financial control plane for money-spending agents**.

When a Hermes agent spends real money via Stripe Skills (buying things, paying
per-call APIs, provisioning SaaS), Argus:

- **Meters** every dollar in and every dollar out, per job.
- **Tracks** live P&L per job from a single ledger.
- **Gates** each spend through a human-in-the-loop (HITL) approval flow when
  it would breach the budget for its cost center.

Argus is **industry-agnostic**: it does not care what the agent does, only
that it spends money that must be controlled. The hackathon demo proves
horizontality by governing three unrelated jobs (a pay-per-call API job, a
SaaS-provisioning job, and a one-off service purchase) with the **same**
control layer.

Hackathon north stars (Nous Research × NVIDIA × Stripe, deadline 2026-06-30):
**usefulness**, **viability**, **presentation**. Every scope decision below
serves those three.

---

## 2. Architecture

Five layers. The **Ledger** is the center; **Policy** never writes; only
**Enforcement** writes runtime state.

| Layer | Role | Writes? | Reads? |
|---|---|---|---|
| Capture (instrumentation) | Argus's own Hermes `pre_tool_call` hook; records money in/out via Stripe Skills. Joins LLM cost from `hermes-telemetry`. | Ledger (revenue, llm_cost, external_spend rows) | telemetry.db (read-only) |
| Ledger (source of truth) | Argus's own SQLite WAL DB. Unified money ledger + cost centers + budgets + approval requests + audit trail. | — (passive store) | — |
| Policy (the brain / gate) | **Pure function**: `(job, projected_spend, ledger_snapshot) → ALLOW \| NEEDS_APPROVAL(level)`. No I/O. Unit-testable. | nothing | Ledger snapshot |
| Enforcement (control) | Argus's pre-execution hook. Acts the verdict: `ALLOW` → proceed; `NEEDS_APPROVAL` → block the Stripe command, create the approval request, hold the agent. On human decision → resume or cancel (refund via Stripe on cancel where applicable). **Only layer that writes runtime state.** | Ledger (approval_requests, audit_trail) | Policy verdict |
| Dashboard (human + observability) | React tab inside the Hermes dashboard. Approval queue (Approve/Reject), P&L view, cost centers, audit trail. | approval decisions (which Enforcement consumes) | Ledger |

```
Capture ─→ Ledger ←─ Policy ←─ Enforcement
                ↑                      ↕
            Dashboard ────────────────┘
   (Capture also reads llm_cost from hermes-telemetry, read-only)
```

**Dependency rule.** Ledger is the center; every layer reads from it.
Policy is pure (no writes). Enforcement is the **only** layer that mutates
runtime state during a request. This makes the system simulable: feed any
historical ledger snapshot into Policy and reproduce the verdict.

### Architecture stance — own plugin, own repo

Argus is its **own** Hermes plugin in its **own** repo. It does **not** add
code to the existing `hermes-telemetry` plugin. It consumes `hermes-telemetry`
as a **read-only data dependency** (for the already-priced LLM token cost)
and otherwise talks to Hermes directly (hooks, dashboard plugin SDK, Stripe
Skills). `hermes-telemetry` stays 100% untouched.

---

## 3. HITL approval flow

**Threshold routing by cost center**, configurable:

| Tier | Action |
|---|---|
| Small spend (≤ low threshold) | Auto-approve |
| Medium spend | Route to manager |
| Large spend (> high threshold) | Route to finance |

**Hold semantics** preserve agent state; on approval, execution resumes from
the exact pre-spend point. On rejection, the spend is cancelled cleanly; if a
charge already settled (rare in the gated path) Argus issues a Stripe refund.

**Enterprise differentiator vs Stripe's per-action limit:** Stripe Skills'
built-in limit is a static $/call ceiling. Argus is **dynamic** (against a
live ledger), **cross-session** (budgets persist across agent runs),
**margin-aware** (decisions consider revenue per job, not just spend),
**observable** (P&L, audit trail), and produces an **auditable record** of
every human decision — what enterprises actually need to let an agent touch
their wallet.

---

## 4. Reuse / dependency map

**From `hermes-telemetry` (READ-ONLY, zero code changes there):**

- DB path: `~/.hermes/telemetry/telemetry.db` (WAL, schema v5).
- Table `runs(session_id PRIMARY KEY, model, provider, tokens_in, tokens_out,
  cost_usd REAL, started_at, ended_at, ...)`. Argus joins on `session_id`.
- Access pattern: open via SQLite URI in read-only mode
  (`file:.../telemetry.db?mode=ro`). WAL allows concurrent readers without
  blocking the writer. Never write, never `ATTACH ... AS rw`.

**From Hermes Agent directly (Argus's own plugin surfaces):**

- **Hook system** — `register_hook("pre_tool_call", fn)` via the plugin
  `register(ctx)` entrypoint. Callback signature:
  `fn(tool_name: str, args: dict, task_id: str, **kwargs)`.
- **Block primitive** — return `{"action": "block", "message": "..."}` from
  `pre_tool_call`; the agent short-circuits the tool and the `message` is
  returned to the model as an error. See §6 for the action-gating decision.
- **Dashboard plugin SDK** — `window.__HERMES_PLUGIN_SDK__` (React, hooks,
  shadcn components, `api`, `fetchJSON`, `utils.cn`, theme CSS vars) and
  `window.__HERMES_PLUGINS__.register(name, Component)`.
- **Stripe Skills for Hermes** — the surface agents call to actually spend.
  Argus's hook matches the Stripe-skill tool name(s) on `pre_tool_call`.
- **Runtime** — Nemotron 3 Ultra / NemoClaw (no changes needed; Argus is
  model-agnostic).

**NEW, built in Argus:**

- Dollar ledger (revenue + external_spend rows).
- Pure policy gate.
- Approval queue + cost-center config + budgets.
- P&L + audit trail dashboard.

---

## 5. Cost-data wiring — the ATTACH decision

We need the already-priced LLM cost from `hermes-telemetry` to join into
Argus's P&L. Two options:

- **A1 (primary).** At P&L query time, open Argus's DB and `ATTACH DATABASE
  'file:~/.hermes/telemetry/telemetry.db?mode=ro' AS telemetry`, then join
  `argus.ledger.session_id = telemetry.runs.session_id`. Zero code in
  `hermes-telemetry`. Read-only across the WAL boundary.
- **A2 (demo-safe fallback).** In Argus's `pre_tool_call` (or
  `on_session_end`) hook, read `runs.cost_usd` for the current session and
  insert an `llm_cost` row into Argus's own ledger. P&L is then a single-DB
  query. This is ETL of a fact, **not** re-measurement of tokens — telemetry
  remains the source of truth for cost.

**Decision: pick A1.** A2 stays documented for demo-day fragility.

---

## 6. Action-gating primitive — ⚠️ the riskiest unknown, resolved

The whole HITL flow depends on being able to **block the Stripe spend before
it settles, hold until a human decides, then resume**. Phase 0 investigation
in `NousResearch/hermes-agent` confirmed:

- A plugin can register `pre_tool_call`, which fires **immediately before**
  any tool executes (`agent/tool_executor.py`,
  `agent/agent_runtime_helpers.py`).
- Returning `{"action": "block", "message": "..."}` short-circuits execution.
  The agent does not run the tool; `message` is returned to the model as an
  error string. *Source: `website/docs/user-guide/features/hooks.md` and
  `hermes_cli/plugins.py`.*

**What Hermes does NOT provide:** a native async "hold this pending action,
park the agent, resume the exact call when a human answers" primitive. Block
is one-shot and synchronous-return.

**What Argus does instead — synchronous hold inside the hook.** The
`pre_tool_call` callback itself blocks: it enqueues the approval request,
then **synchronously waits** (polling Argus's own DB with a short interval
and a configurable timeout) for the human decision. On decision:

- **Approve** → return `None` (or any non-block value). Hermes proceeds to
  execute the Stripe skill exactly as if the hook were a no-op.
- **Reject / timeout** → return `{"action": "block", "message": "<reason>"}`.
  The agent receives the rejection as an error and can self-correct.

This gives us a real hold/resume without modifying Hermes core. Trade-offs
are explicit:

- The hook thread is parked for the approval duration. Acceptable: this is a
  human-gated path, not a hot loop, and Hermes's tool-execution path is
  per-call.
- Hermes turn-level timeouts (if any) bound the approval window. We will
  surface a configurable timeout (default 5 min for the demo) and treat
  timeout as implicit rejection.
- If a future Hermes release adds a richer "park + resume" API, Argus can
  swap implementations behind the same `Policy → Enforcement` interface
  without changing the ledger or dashboard.

**This finding is design-load-bearing.** If during implementation we discover
the hook thread cannot block long enough, the fallback is the demo-safe
A2-style flow: pre-declare projected spend, decide BEFORE the spend tool is
invoked at all (e.g. via a thin `request_spend(...)` skill the agent must
call first), and gate at that layer.

---

## 7. View-layer stack (LOCKED)

- React, no app framework. **No Next.js** (no SSR, no router, no standalone
  app).
- Bundler: **esbuild**, single IIFE output at `dashboard/dist/index.js`.
- Mounting: reads `window.__HERMES_PLUGIN_SDK__` for React + hooks +
  components; calls `window.__HERMES_PLUGINS__.register("argus", <Component>)`.
- Styling: SDK theme CSS variables only (`--color-card`,
  `--color-primary`, `--color-destructive`, `--radius`, etc.). **No
  hardcoded colors.**
- Data: `dashboard/plugin_api.py` (FastAPI router), routes mounted at
  `/api/plugins/argus/`.
- Live updates: **polling 1–2s** for the demo. SSE is a stretch goal only.

Install layout (matches the official example):

```
~/.hermes/plugins/argus/
├── dashboard/
│   ├── manifest.json
│   ├── dist/
│   │   ├── index.js     # built IIFE — never hand-edited
│   │   └── style.css    # optional
│   └── plugin_api.py
├── plugin.yaml          # Argus's Hermes plugin manifest (Python side)
└── __init__.py          # register(ctx) entrypoint (Phase 2: stub)
```

Rescan after install: `GET /api/dashboard/plugins/rescan`.

---

## 8. Planned ledger schema (DESIGN ONLY — implemented next session)

```sql
CREATE TABLE ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,                -- ISO-8601
    job_id      TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN
                  ('revenue', 'llm_cost', 'external_spend')),
    amount_usd  REAL    NOT NULL,
    source      TEXT,                            -- stripe | openrouter | nim | ...
    ref         TEXT                             -- stripe payment id, model id, etc.
);
CREATE INDEX ledger_job_idx ON ledger(job_id);

CREATE TABLE cost_centers (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL
);

CREATE TABLE budgets (
    cost_center_id  TEXT PRIMARY KEY REFERENCES cost_centers(id),
    limit_usd       REAL NOT NULL,
    soft_threshold  REAL NOT NULL DEFAULT 0.8,   -- warn at 80%
    auto_approve_under_usd  REAL NOT NULL DEFAULT 0.0,
    manager_under_usd       REAL                 -- NULL → straight to finance above auto threshold
);

CREATE TABLE approval_requests (
    id              TEXT PRIMARY KEY,            -- uuid
    created_at      TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    cost_center_id  TEXT NOT NULL,
    projected_usd   REAL NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                      ('pending','approved','rejected','timeout')),
    decided_at      TEXT,
    decided_by      TEXT,
    reason          TEXT
);

CREATE TABLE audit_trail (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,                   -- 'agent' | 'human:<id>' | 'system'
    event       TEXT NOT NULL,                   -- 'spend_attempted', 'approval_requested', 'approved', ...
    payload     TEXT                             -- JSON
);
```

P&L per job:

```sql
SELECT job_id,
       SUM(CASE kind WHEN 'revenue'        THEN amount_usd ELSE 0 END)
     - SUM(CASE kind WHEN 'llm_cost'       THEN amount_usd ELSE 0 END)
     - SUM(CASE kind WHEN 'external_spend' THEN amount_usd ELSE 0 END) AS pnl_usd
  FROM ledger
 GROUP BY job_id;
```

`llm_cost` is populated under A1 by joining `telemetry.runs` via ATTACH at
query time; under A2 it's an actual ledger row written by Capture.

**Attribution chain:** `session → job → cost_center`. Sessions come from
Hermes (`task_id` / telemetry `session_id`); `job_id` is declared by the
agent (or derived from cron job id); `cost_center_id` is mapped from
`job_id` via config. This gives per-center chargeback and per-center
budgets.

---

## 9. Three open design decisions

These remain **OPEN** with a current leaning; lock them in the next session.

1. **How is `projected_usd` known before the spend?**
   Options: (a) parse the Stripe-skill `args` for an amount, (b) read an
   HTTP 402 challenge response, (c) require the agent to call a thin
   `request_spend(job_id, projected_usd, ref)` skill first.
   **Leaning: (c)** for the demo — explicit declaration is robust, demo-able,
   and survives Stripe API surface drift.

2. **How does revenue enter the ledger?**
   In Stripe TEST mode, a Stripe webhook (or a simple simulation script for
   the demo) writes a `revenue` row tied to a `job_id`. The webhook receiver
   lives under `plugin_api.py`.

3. **How is a spend tied to a cost center?**
   Via the `session → job → cost_center` chain. `job_id` is supplied by the
   agent in step 1 above; the mapping `job_id → cost_center_id` is read from
   a config file shipped with the plugin (`cost_centers.yaml`).

---

## 10. Hackathon constraints (non-functional)

- **Stripe Link CLI is US-only** → use **Stripe TEST/sandbox mode** for every
  demo flow. No production keys.
- **Submission deadline: 2026-06-30** → scope is **LOCKED** to what this
  document describes. No features beyond §1–§9. New ideas land in a
  `FUTURE.md`, not in code.
- **Demo script:** three unrelated jobs — pay-per-call API, provision+pay
  SaaS, one-off service purchase — all governed by the **same** Argus
  control plane. Climactic beat: an approval card appears in the dashboard
  and a human Approves/Rejects in real time, the agent visibly resumes (or
  cancels) from the held tool call.

- **NVIDIA pillar — not free, must be earned in the demo.** Argus's code
  is model-agnostic, which means the NVIDIA half of the brief
  (NemoClaw / Nemotron 3 Ultra / NVIDIA agent skills) only counts if the
  **demo wiring** uses them. Phase 4 deliverables that lock this in:
  1. The three demo agents run on **Nemotron 3 Ultra via NemoClaw** —
     configured in Hermes (not Argus). This makes the `llm_cost` column
     in P&L specifically Nemotron-priced, surfaced by Argus's read-only
     ATTACH to `hermes-telemetry`.
  2. **At least one of the three jobs spends money on an NVIDIA surface**
     — e.g. pays for a NIM inference endpoint, provisions a NeMo service,
     or invokes a paid NVIDIA agent skill — so NVIDIA appears in the
     `external_spend` ledger rows, not just in the runtime.
  3. **Writeup line:** "Argus gates spend regardless of what the agent
     does — the demo shows it governing three Hermes agents running on
     Nemotron 3 Ultra through NemoClaw, each touching different
     NVIDIA / SaaS / Stripe surfaces."

  None of this requires Argus code changes; it's demo orchestration. But
  if Phase 4 ships the gating flow without these three, the NVIDIA pillar
  is decorative and judges will notice.

---

## 11. Guardrails for future sessions

- **Never modify `hermes-telemetry`.** It is a read-only dependency.
- **Never bundle React.** Always use `window.__HERMES_PLUGIN_SDK__.React`.
- **Never hardcode colors.** Use SDK theme CSS variables only.
- **Policy stays pure.** No I/O, no clock, no randomness — inputs in,
  verdict out. This is what lets us unit-test the brain.
- **Only Enforcement writes runtime state.** Capture writes facts; everything
  else reads.
- **If the official Hermes example contradicts this document, the example
  wins** — update this doc in the same commit.
