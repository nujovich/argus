# Argus

> **Stripe gives the wallet. NemoClaw isolates the process. NVIDIA sells
> the compute. Argus allocates all three as capital, in real time,
> toward margin.**

Argus Panoptes — the hundred-eyed guardian of Greek myth — reframed for
the agent era: a hundred eyes on every unit of capital flowing through a
fleet of autonomous Hermes agents, in **both** currencies that matter —
cash and compute.

![Argus dashboard — Mermelada Studio commission, real Stripe + Nemotron + defense in depth](docs/pnl-final-real.webp)

---

## What Argus is

Argus is the **economic operating system for an AI factory**. It's a
Hermes plugin that turns a fleet of autonomous agents into a margin-
aware business.

Every agent consumes two fungible capitals:

- **Cash** — Stripe Skills: buys things, pays per-call APIs, provisions
  SaaS.
- **Compute** — NVIDIA Nemotron tokens, NIM inference cycles, GPU.

Today every agent demo treats inference as free or fixed. That's the
error. Compute *is* money — `hermes-telemetry` already prices Nemotron
sessions in dollars; Argus reads that ledger directly and governs both
capitals through one engine:

1. **Meters** every dollar of cash and compute, per job, into a unified
   SQLite WAL ledger.
2. **Allocates** Nemotron tier per job toward margin. High-value jobs
   earn Ultra. Thin-margin jobs are downgraded to Base, queued, or
   rejected.
3. **Throttles mid-flight.** As actual compute burn erodes a job's
   projected margin, Argus emits a downgrade order; the agent switches
   to a cheaper model on the next turn.
4. **Enforces** in two layers on the cash side — the `pre_tool_call`
   hook (in-process) plus the Stripe Issuing authorization webhook
   (card network). A rogue agent doesn't get past either.
5. **Audits** every capital decision in a hash-chained trail. Production-
   grade evidence for a CFO and an ops team.

Argus is **horizontal**: it does not care what the agent does. It cares
that the agent spends capital that must be allocated toward margin and
controlled.

## Why it matters

Stripe's own spend controls — per-action ceilings, per-provider caps —
are **static, per-provider, spend-only** guardrails: a cap knows nothing
about the job it's serving. Argus operates one level up:

- **Dynamic** — decisions run against a live ledger, not a fixed
  ceiling.
- **Cross-session** — budgets persist across agent runs, not per-call.
- **Cross-provider** — one cost center spans every Stripe skill and
  every NVIDIA surface.
- **Margin-aware** — weighs revenue per job, not just spend.
- **Auditable** — every human decision recorded.

Stripe answers *"can this single call afford it?"* Argus answers
*"is this job still profitable, and who approved the spend?"* — which is
what enterprises actually need before they let an agent touch their
wallet.

---

## Architecture — six layers, Ledger at the center

```
        Capture ─→ Ledger ←─ Policy ←─ Enforcement
                     ↑           ↑              ↕
                     │           │
                     │     Compute Allocator
                     │           ↕
                Dashboard ───────┘
       (Capture also reads llm_cost from hermes-telemetry, read-only)
```

| Layer | Role |
|---|---|
| **Capture** | `pre_tool_call` / `post_tool_call` hooks + Stripe webhooks. Writes revenue, external spend, declarations. |
| **Ledger** | SQLite WAL DB. Unified cash + compute ledger, cost centers, budgets, audit, tokens. |
| **Policy** | **Pure function**: `(declaration, snapshot) → Verdict ∈ {ALLOW, NEEDS_APPROVAL, TIER_ASSIGNED, REJECT}`. No I/O, no clock. |
| **Enforcement** | The hook (Layer 1, in-process) + the Stripe Issuing authorization webhook (Layer 2, card network). Fails **closed**. |
| **Compute Allocator** | Assigns the Nemotron tier per job, re-evaluates each turn, emits downgrade orders. |
| **Dashboard** | React tab inside Hermes. Workflow timeline, fleet view, approvals, P&L, token vault, live event stream. |

**Dependency rule.** Ledger is the center. Policy is pure. Enforcement
is the only writer of cash decisions; Compute Allocator is the only
writer of compute-tier decisions. Both write through the same audit
trail. The full design lives in [`CLAUDE.md`](./CLAUDE.md) — the single
source of truth.

## The two enforcement loops

**Cash — two-layer enforcement.**

- *Layer 1 (in-process):* the `pre_tool_call` hook matches spend
  commands (`stripe projects add/upgrade`, `mpp pay`, `stripe-link-cli`
  flows) and requires a valid Argus auth token in
  `args.metadata.argus_auth_token`. No token → BLOCK.
- *Layer 2 (network):* `POST /webhooks/stripe-issuing-authorization`
  validates the same token at the card network. Even an agent that
  bypasses Hermes entirely is declined.

**Compute — two-layer enforcement.**

- *Layer 1 (declaration gate):* the agent calls
  `argus-request-compute(job_id, expected_revenue_usd, projected_burn_usd)`.
  Argus assigns a tier (Ultra / Base / Reject) and a compute budget,
  and returns a token encoding `(model, budget)`.
- *Layer 2 (integrity sweep):* Argus periodically diffs
  `hermes-telemetry.runs.model` against what the token authorized. Any
  mismatch is logged as `compute_integrity_violation`.

Auth tokens are 60-second, single-use, anchored to
`(job_id, cost_center_id, amount, ±10% tolerance)`.

## Status

**Engine complete.** The five logic layers (Ledger, Policy, Enforcement,
Capture, Compute Allocator) plus signature-verified revenue intake are
implemented and tested — full suite green (162 passing). The React
Dashboard UI (§7 of [`CLAUDE.md`](./CLAUDE.md)) is the only remaining
piece.

---

## Run the tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

## Demo without an agent

The dashboard hits the same code path the hook does, via
`POST /api/plugins/argus/sim/spend`. Useful for development:

```bash
curl -X POST http://127.0.0.1:9119/api/plugins/argus/sim/spend \
  -H 'content-type: application/json' \
  -d '{"job_id":"demo","cost_center_id":"default","projected_usd":5.0}'
```

A pending approval will appear in the dashboard — click Approve or
Reject. The full reproducible recipe is in [`DEMO.md`](./DEMO.md).

## Install (dev)

```bash
# 1. Build the frontend bundle
npm install
npm run build

# 2. Symlink into Hermes
ln -s "$PWD" ~/.hermes/plugins/argus

# 3. Tell the running dashboard to rescan
curl http://127.0.0.1:9119/api/dashboard/plugins/rescan
```

Open the dashboard; the **Argus** tab appears at the end.

## Layout

```
argus/
├── CLAUDE.md           # design doc — single source of truth
├── DEMO.md             # reproducible demo recipe
├── SUBMISSION.md       # hackathon writeup
├── FUTURE.md           # post-deadline roadmap
├── plugin.yaml         # Hermes plugin manifest (Python side)
├── __init__.py         # register(ctx) → wires hooks + revenue intake
├── capture.py          # Capture layer (pre/post tool, webhooks)
├── enforcement.py      # Enforcement (synchronous hold, fails closed)
├── policy.py           # pure decide() function
├── db.py               # ledger + approvals + audit (SQLite WAL)
├── matchers.py         # Stripe spend-command patterns
├── config.py           # paths and cost-center loading
├── schema.sql          # ledger schema (ledger, approvals, audit, tokens, …)
├── cost_centers.yaml.example
├── skills/             # argus-request-spend, argus-request-compute
├── examples/           # mermelada-studio reference agent
├── dashboard/
│   ├── manifest.json
│   ├── plugin_api.py   # FastAPI router → /api/plugins/argus/
│   └── dist/index.js   # BUILT IIFE — do not hand-edit
├── src/                # React source for the tab
├── build.mjs           # esbuild → dashboard/dist/index.js
├── site/               # standalone Astro landing page
└── tests/
```

## Further reading

- [`CLAUDE.md`](./CLAUDE.md) — design doc and single source of truth.
- [`DEMO.md`](./DEMO.md) — reproducible demo recipe.
- [`SUBMISSION.md`](./SUBMISSION.md) — hackathon writeup, with the
  Mermelada Studio narrative beat by beat.
- [`FUTURE.md`](./FUTURE.md) — what's explicitly out of scope for v1.
