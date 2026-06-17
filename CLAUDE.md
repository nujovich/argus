# Argus — design doc (CLAUDE.md)

> Single source of truth for design decisions in this repo. All future work
> must respect this document; deviations require an explicit update here.
>
> Status: **Phase 4 — demo orchestration in flight.** Core ledger / policy /
> dashboard + Stripe Layer 1 + Layer 2 hook ship. Compute tier gate +
> fleet view are Phase 4.5 (in-flight; see §6).

Name story: **Argus Panoptes**, the hundred-eyed guardian of Greek myth —
now reframed for the agent era: a hundred eyes on every unit of capital
flowing through a fleet of autonomous agents, in **both** currencies that
matter — cash and compute.

Tagline: *Stripe gives the wallet. NemoClaw isolates the process. NVIDIA
sells the compute. Argus allocates all three as capital, in real time,
toward margin.*

---

## 1. Product thesis

Argus is the **economic operating system for an AI factory**.

Every autonomous agent consumes two fungible capitals:

- **Cash** (Stripe — buys things, pays per-call APIs, provisions SaaS).
- **Compute** (NVIDIA — burns Nemotron tokens, NIM inference cycles, GPU).

Today every agent demo treats inference as free or fixed. **That's the
error.** Compute is money — `hermes-telemetry` already prices Nemotron
sessions in dollars; Argus reads that ledger directly.

Argus's job:

1. **Meters** every dollar of both capitals, per job, into a unified ledger.
2. **Allocates** compute and cash across a fleet of Hermes agents toward
   margin. High-value jobs earn premium tiers (Nemotron 3 Ultra on
   NemoClaw). Low-value jobs get downgraded to a cheaper model, queued,
   or rejected.
3. **Throttles mid-flight.** As actual compute burn erodes a job's
   projected margin, Argus emits a downgrade order; the agent switches
   to a cheaper model on the next turn. Real compute capital allocation,
   not a static $/call limit.
4. **Enforces** with teeth on both layers:
   - **Cash:** the Stripe Skills hook (Layer 1) + Stripe Issuing
     authorization webhook (Layer 2). A rogue agent doesn't get past the
     card network.
   - **Compute:** the `argus-request-compute` declaration gate +
     compute-integrity inspection against `hermes-telemetry` (catch when
     a session said "ok @ Ultra" but silently fell back to a cheaper
     model — flag the discrepancy).
5. **Audits** every capital allocation decision in a hash-chained trail.
   Production-grade evidence for a CFO + ops team.

Argus is **horizontal**: it does not care what the agent does. It cares
that the agent spends capital that must be allocated toward margin and
controlled. The hackathon demo proves this by running a **fleet of three
unrelated jobs** through the same allocator, each with a different
margin profile and a different compute tier outcome.

Hackathon north stars (Nous Research × NVIDIA × Stripe, deadline
2026-06-30): **usefulness**, **viability**, **presentation**. The pivot
to "AI factory OS" hits all three sponsors in their nerve:

- **Stripe**: Argus is the spend governance plane Stripe Skills needs
  to ship to enterprise (Layer 1 hook + Layer 2 Issuing).
- **Nous**: Argus governs the Hermes fleet, with `hermes-telemetry` as
  a read-only data dependency.
- **NVIDIA**: Argus allocates **compute** as capital. Premium jobs run
  on Nemotron 3 Ultra. Low-margin jobs get downgraded. NVIDIA is at
  the center of the value flow — not a passive runtime.

---

## 2. Architecture

Six layers. The **Ledger** is the center. **Policy** never writes. Only
**Enforcement** mutates runtime state. The **Compute Allocator** is the
new mechanism that the AI-factory framing rests on.

| Layer | Role | Writes? | Reads? |
|---|---|---|---|
| Capture | `pre_tool_call` hook + Stripe webhooks + revenue declarations + compute declarations. | Ledger (revenue, llm_cost, external_spend), audit, auth_tokens | telemetry.db (RO) |
| Ledger | Argus's SQLite WAL DB. Unified cash + compute ledger, cost centers, budgets, approvals, audit, tokens. | — (passive store) | — |
| Policy | **Pure function**: `(declaration, snapshot) → Verdict` where Verdict ∈ {ALLOW, NEEDS_APPROVAL(level), TIER_ASSIGNED(model, budget), REJECT}. No I/O, no clock. | nothing | Ledger snapshot |
| Enforcement | The hook + the Stripe Issuing authorization webhook. **Two layers**: in-process (hook) + network (card auth callback). | Ledger (approvals, audit, tokens) | Policy verdict |
| Compute Allocator | Decides which Nemotron tier a job is authorized to consume. Re-evaluates each turn as compute burn accrues. Emits downgrade orders. | Ledger (compute_allocations table), audit | Ledger, telemetry.runs |
| Dashboard | React tab inside Hermes. Workflow timeline + fleet view + approval queue + P&L + token vault + live event stream. | approval decisions | Ledger |

```
        Capture ─→ Ledger ←─ Policy ←─ Enforcement
                     ↑           ↑              ↕
                     │           │
                     │     Compute Allocator
                     │           ↕
                Dashboard ───────┘
       (Capture also reads llm_cost from hermes-telemetry, read-only)
```

**Dependency rule.** Ledger is the center. Policy is pure. Enforcement
is the only runtime-state writer for cash decisions; Compute Allocator
is the only writer for compute-tier decisions. Both write through the
same audit trail.

### Architecture stance — own plugin, own repo

Argus is its **own** Hermes plugin in its **own** repo. It does **not**
add code to the existing `hermes-telemetry` plugin. It consumes
`hermes-telemetry` as a **read-only data dependency** (for per-session
Nemotron pricing) and otherwise talks to Hermes directly (hooks,
dashboard plugin SDK, Stripe Skills).

---

## 3. The two enforcement loops

### 3.1 Cash — two-layer enforcement

Same as before; the existing implementation is the foundation. Summary:

- **Layer 1 (in-process):** `pre_tool_call` hook intercepts `stripe_*`
  invocations. Requires a valid Argus auth token in
  `args.metadata.argus_auth_token`. Without it → block.
- **Layer 2 (network):** `POST /webhooks/stripe-issuing-authorization`
  validates the auth token at the card network. Even an agent that
  bypasses Hermes entirely is declined.

See §6 for token semantics.

### 3.2 Compute — two-layer enforcement

The new mechanism. Symmetric to cash:

- **Layer 1 (declaration gate):** the agent calls
  `argus-request-compute(job_id, expected_revenue_usd, projected_burn_usd)`
  before starting an LLM-heavy operation. Argus assigns a tier:

  | Verdict | Output |
  |---|---|
  | `TIER_ULTRA` | model = `nvidia/nemotron-3-ultra-550b-a55b`, compute_budget = projected_burn (margin-justified) |
  | `TIER_BASE` | model = `nvidia/nemotron-3-base-9b` (or equivalent cheap NVIDIA), compute_budget = capped |
  | `NEEDS_APPROVAL_MANAGER` | over compute-budget threshold for the cost center |
  | `REJECT` | margin would be negative |

  The agent must use the assigned model. The auth token returned
  encodes the model + budget; subsequent Nemotron API calls are
  validated against it.

- **Layer 2 (compute integrity):** Argus periodically diffs
  `hermes-telemetry.runs.model` against what its tokens authorized.
  Any mismatch (Hermes silently fell back to a cheaper model, or an
  agent ran on a tier it wasn't authorized for) is logged as
  `compute_integrity_violation` in the audit trail and surfaced on the
  dashboard. *This is the "silent fallback" defense the user already
  has detection for in another context — it ports directly.*

### 3.3 Mid-flight downgrade (compute throttling)

The job declares `(expected_revenue, projected_burn)` at start. Argus
allocates a tier and a compute_budget. As the agent runs and telemetry
accumulates `llm_cost` against the job's `session_id`, Argus
recomputes:

```
margin_so_far = revenue_so_far - llm_cost_so_far - external_spend_so_far
burn_ratio    = llm_cost_so_far / projected_burn
```

Thresholds (configurable per cost center):

- `burn_ratio > 0.7` and `tier == Ultra` → emit `downgrade_to_base`
  order. The agent reads `GET /jobs/{job_id}/status` each turn and
  switches model on the next turn.
- `margin_so_far < 0` → emit `kill` order. The agent pauses; the human
  decides whether to approve continuation or terminate.

This is the line that wins the NVIDIA pillar: **Argus allocates GPU as
capital, in real time, mid-flight.**

---

## 4. HITL approval flow

Threshold routing per cost center, configurable. Cash and compute use
the same approval queue (different verdict types).

| Tier | Action |
|---|---|
| Small spend (≤ low threshold) / margin-justified Ultra | Auto-approve |
| Medium spend / Ultra above projected budget | Route to manager |
| Large spend / Ultra requested on low-margin job | Route to finance |

Hold semantics preserve agent state; on approval, execution resumes
from the exact pre-spend or pre-inference point. On rejection, the
agent gets a structured block and self-corrects (smaller spend, cheaper
tier, different approach).

**Enterprise differentiator vs Stripe's per-action limit:** Stripe's
built-in limit is a static $/call ceiling on cash. Argus is **dynamic**
(against a live ledger), **cross-session** (budgets persist across
agent runs), **margin-aware** (compute tier is allocated by revenue
expectation), **dual-currency** (governs cash and compute through the
same engine), **observable** (P&L + fleet view), and produces an
**auditable record** of every capital allocation — what enterprises
need before they let a fleet of agents loose on their balance sheet.

---

## 5. Reuse / dependency map

**From `hermes-telemetry` (READ-ONLY, zero code changes there):**

- DB path: `~/.hermes/telemetry/telemetry.db` (WAL, schema v5).
- Table `runs(session_id PRIMARY KEY, model, provider, tokens_in,
  tokens_out, cost_usd REAL, started_at, ended_at, status, ...)`.
- Access via SQLite URI read-only mode
  (`file:.../telemetry.db?mode=ro&uri=true`). WAL allows concurrent
  readers without blocking the writer.
- The `model` column is the input to Argus's compute-integrity check
  (compare what the agent ran on vs what the token authorized).

**From Hermes Agent directly (Argus's own plugin surfaces):**

- **Hook system** — `register_hook("pre_tool_call", fn)`. Sync block
  is the gating primitive (see §6).
- **Skills** — `argus-request-spend` and `argus-request-compute` ship
  as local Hermes skills the agent invokes via the `terminal` tool to
  curl Argus's API.
- **Dashboard plugin SDK** — `window.__HERMES_PLUGIN_SDK__` (React +
  hooks + shadcn components + theme CSS vars + `fetchJSON`).
- **Stripe Skills for Hermes** — the surface Argus intercepts via the
  `stripe_*` prefix on the hook.
- **Runtime** — Nemotron 3 Ultra via NVIDIA's API (configured by the
  user in `hermes model`). NemoClaw is the safe-execution
  environment a production deployment would run inside.

**NEW, built in Argus:**

- Dollar ledger (revenue + llm_cost + external_spend rows).
- Pure policy gate (cash verdicts + compute tier verdicts).
- Approval queue + cost-center config + dual-currency budgets.
- Auth tokens (short-lived, single-use, validated at hook + Issuing).
- Compute Allocator (Phase 4.5).
- Fleet view + workflow timeline + P&L + token vault dashboard.

---

## 6. Action-gating primitive — synchronous hold inside the hook

The whole HITL flow depends on blocking the Stripe spend before it
settles, holding until a human decides, then resuming. Phase 0
investigation in `NousResearch/hermes-agent` confirmed:

- A plugin can register `pre_tool_call`, fired immediately before any
  tool executes.
- Returning `{"action": "block", "message": "..."}` short-circuits
  execution. The agent does not run the tool; `message` is returned
  to the model as an error.

**What Hermes does NOT provide:** a native async "hold this pending
action, park the agent, resume the exact call when a human answers"
primitive. Block is one-shot and synchronous-return.

**What Argus does instead — synchronous hold inside the hook.** The
`pre_tool_call` callback itself blocks: it enqueues the approval
request, then **synchronously waits** (polling Argus's own DB with a
short interval and a configurable timeout) for the human decision. On
decision:

- **Approve** → issue auth token, return `None`. Hermes proceeds.
- **Reject / timeout** → return `{"action": "block", "message": ...}`.

For **compute**, the same primitive applies through the declaration
skill (`argus-request-compute` calls the same gate via `/sim/spend`
or its compute equivalent endpoint). The agent's subsequent Nemotron
call carries the auth token in metadata; if it doesn't, the Stripe-
side Layer 2 (Issuing webhook for cash) and the compute-integrity
check (audit trail diff against telemetry) catch it.

### Auth tokens (defense in depth)

Every ALLOW issues a 60-second single-use token, anchored to
(job_id, cost_center_id, amount, ±10% tolerance). The token is
validated:

- At the in-process hook (Layer 1) for every `stripe_*` tool call.
- At the Issuing authorization webhook (Layer 2) for every card auth
  request from Stripe.
- At the compute-integrity check (post-hoc) for every priced telemetry
  session — if the model differs from what the token authorized, it's
  a violation.

---

## 7. View-layer stack (LOCKED)

- React, no app framework. **No Next.js**.
- Bundler: **esbuild**, single IIFE at `dashboard/dist/index.js`.
- Mounting: reads `window.__HERMES_PLUGIN_SDK__` for React + hooks +
  components; registers via `window.__HERMES_PLUGINS__.register("argus", <Component>)`.
- Styling: SDK theme CSS variables only. **No hardcoded colors.**
- Data: `dashboard/plugin_api.py` (FastAPI), routes at
  `/api/plugins/argus/`.
- Live updates: **polling 0.8s–1.5s** for the demo. SSE is a stretch.

Install layout (matches the official example):

```
~/.hermes/plugins/argus/
├── dashboard/
│   ├── manifest.json
│   ├── dist/
│   │   ├── index.js     # built IIFE
│   │   └── style.css    # optional
│   └── plugin_api.py
├── plugin.yaml          # Hermes plugin manifest (Python side)
└── __init__.py          # register(ctx) entrypoint
```

Rescan after install: `GET /api/dashboard/plugins/rescan`.

### Dashboard surfaces (Phase 4 + 4.5)

Single tab, workflow-first. Top to bottom:

1. **Fleet timeline** — every active job's stage progression. Pulses
   on stage change. Color-tinted (pending / human-required / done /
   rejected / blocked).
2. **Start commission + Reset demo** — single-click controls.
3. **Live P&L tiles** — Revenue, LLM cost (Nemotron-priced), External
   spend, Net P&L. Animated number-tick on changes.
4. **Per-tier compute allocation widget** (Phase 4.5) — for each
   active job, current tier (Ultra / Base / Reject), compute_budget,
   actual burn, burn ratio, downgrade-order state.
5. **Approval queue** — cash + compute approvals, pulsing cards.
6. **Active auth tokens** — defense in depth visible.
7. **Live event stream** — audit trail with color-coded badges
   (REVENUE / APPROVED / REJECTED / 🚨 rogue blocked /
   compute_tier_assigned / compute_integrity_violation / etc).

---

## 8. Planned ledger schema (IMPLEMENTED + Phase 4.5 extensions)

Already in code:

```sql
CREATE TABLE ledger (
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

CREATE TABLE approval_requests (...);   -- pending / approved / rejected / timeout
CREATE TABLE audit_trail (...);
CREATE TABLE auth_tokens (...);          -- 60s single-use, defense in depth
```

Phase 4.5 additions (compute allocator):

```sql
CREATE TABLE compute_allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    tier            TEXT NOT NULL CHECK (tier IN ('ultra','base','reject')),
    model           TEXT NOT NULL,
    compute_budget  REAL NOT NULL,
    expected_revenue REAL,
    expected_margin REAL,
    status          TEXT NOT NULL CHECK (status IN
                      ('active','downgraded','killed','done')),
    auth_token      TEXT
);
CREATE INDEX compute_alloc_job_idx ON compute_allocations(job_id);
```

P&L per job (unchanged):

```sql
SELECT job_id,
       SUM(CASE kind WHEN 'revenue'        THEN amount_usd ELSE 0 END)
     - SUM(CASE kind WHEN 'llm_cost'       THEN amount_usd ELSE 0 END)
     - SUM(CASE kind WHEN 'external_spend' THEN amount_usd ELSE 0 END) AS pnl_usd
  FROM ledger
 GROUP BY job_id;
```

`llm_cost` is populated by joining `telemetry.runs` via read-only
`ATTACH` at query time.

**Attribution chain:** `session → job → cost_center → compute_tier`.
Sessions come from Hermes (`task_id` / telemetry `session_id`);
`job_id` is declared by the agent; `cost_center_id` is mapped via
config; `compute_tier` is assigned by Argus and validated against
telemetry post-hoc.

---

## 9. Design decisions

**LOCKED:**

1. `projected_usd` for cash comes from explicit
   `argus-request-spend(...)` declarations the agent makes. Same shape
   for `expected_revenue` + `projected_burn` for compute via
   `argus-request-compute(...)`.
2. Revenue enters via real Stripe webhooks (Payment Link in TEST
   mode for the demo; production via Stripe Connect).
3. `session → job → cost_center → compute_tier` is the unified
   attribution chain.
4. Auth tokens are 60-second single-use, anchored to amount + job.
5. The compute tier ladder for v1: Ultra (Nemotron 3 Ultra 550B) /
   Base (any cheaper Nemotron / NIM / openrouter equivalent) /
   Reject. Bigger ladders are Phase 5.

**OPEN, decide before Phase 4.5 ships:**

6. **Compute-integrity inspection cadence.** Real-time (every audit
   trail write) vs periodic (every 30s sweep). Lean: periodic, with
   immediate post-job inspection on `spend_resumed`.
7. **Mid-flight downgrade enforcement.** Cooperative (agent reads
   `/jobs/{job_id}/status`, switches models) vs hard kill (block
   next `stripe_*` or compute auth). Lean: cooperative for v1,
   document hard kill as Phase 5.

---

## 10. Hackathon constraints (non-functional)

- **Stripe Link CLI is US-only** → use **Stripe TEST/sandbox mode**
  for every demo flow.
- **Submission deadline: 2026-06-30** → scope is **LOCKED** to what
  this document describes. New ideas land in [`FUTURE.md`](./FUTURE.md).

### Demo script — the AI factory floor

Three jobs from an autonomous AI services firm, each with a different
margin profile. All governed by the same Argus.

- **Job A — premium enterprise research ($200 commission)**.
  Agent calls `argus-request-compute(expected_revenue=200, projected_burn=15)`.
  Argus assigns **Nemotron 3 Ultra**. Agent runs deep multi-turn
  research, consumes ~$15 of Ultra inference, delivers, **margin +$185
  visible in P&L with Nemotron-priced LLM column**.
- **Job B — low-margin generation ($3 commission)**.
  Agent calls `argus-request-compute(expected_revenue=3, projected_burn=5)`.
  Argus computes negative margin → **downgrades to Base tier**. Audit:
  `compute_tier_downgraded`. Agent runs on cheap model, consumes
  $0.30, delivers, margin +$2.70. Beat narrative: *"the agent didn't
  get to burn $5 of Ultra on a $3 job."*
- **Job C — mid-flight throttle**.
  Agent declared `projected_burn=$5`, but actual burn races to $4
  while only halfway through. Argus emits `downgrade_to_base`. Agent
  switches model on next turn. Audit: `mid_flight_throttle`.
- **Beat 4 — cash teeth (rogue defense)**.
  An adversarial scenario: an injected agent tries to issue a
  `stripe_create_payment_intent` without an auth token. Layer 1 hook
  blocks. Audit: `🚨 stripe_blocked_no_token`. *"The card network
  declines what Argus didn't authorize, even if the agent goes
  around Hermes."*

Closing line for the screencast:

> **Stripe gives the wallet. NemoClaw isolates the process. NVIDIA
> sells the compute. Argus allocates all three as capital, in real
> time, toward margin. Deploy it on DGX Spark.**

### NVIDIA pillar — earned, not decorative

| Pillar | How Argus earns it |
|---|---|
| **Nemotron 3 Ultra** | Allocated as the **premium tier** for high-margin jobs. Real cost, surfaced in P&L via `hermes-telemetry` ATTACH. |
| **NemoClaw** | Production deployment target. Argus's plugin runs identically inside a NemoClaw sandbox — `0-line code change`. Documented in `FUTURE.md` as the operational path. |
| **NVIDIA agent skills + NIM** | The cheap-tier compute target. Job B routes here when margin is too thin for Ultra. NIM inference credits also show up as `external_spend` rows. |

---

## 11. Guardrails for future sessions

- **Never modify `hermes-telemetry`.** Read-only dependency.
- **Never bundle React.** Use `window.__HERMES_PLUGIN_SDK__.React`.
- **Never hardcode colors.** Use SDK theme CSS variables only.
- **Policy stays pure.** No I/O, no clock, no randomness. Inputs in,
  verdict out. Unit-testable.
- **Only Enforcement + Compute Allocator write runtime state.**
  Capture writes facts; everything else reads.
- **If the official Hermes example contradicts this document, the
  example wins** — update this doc in the same commit.
- **Both currencies, one engine.** Anything that meters or gates only
  cash or only compute is a regression. The unified ledger + dual
  verdict types are the architectural invariant.
