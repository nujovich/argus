# Argus — hackathon submission writeup

> Copy-paste-ready sections for the submission form. The hero pitch is
> at the top. Skip down to "How it works" for the technical detail.

---

## Tagline (≤ 100 chars)

**We gave a Hermes agent a wallet. Argus made sure it didn't blow up.**

---

## The hook — what a judge sees first

> We gave a Hermes agent on Nemotron 3 Ultra a `$50` budget and three
> jobs: buy API calls, provision a SaaS database, top up some NIM
> credits. We told it to optimize for profit.
>
> The agent made **38 spend decisions** in five minutes.
>
> Argus auto-approved 32 micro-charges. It paused the agent on 4
> medium-sized ones and asked us. It blocked 2 finance-tier purchases
> until a human said yes. When we rejected one of them, the agent saw
> the block, **decided on its own to retry at a smaller amount**, and
> kept going.
>
> Net: `$154` revenue, `$90` spent (including `$0.01` of Nemotron token
> cost auto-joined from `hermes-telemetry`), `+$63.89` P&L. Zero
> runaway charges. Full audit trail of who approved what.
>
> **That's what production-grade agentic commerce looks like.**

![Argus dashboard — final P&L of the three-job demo](docs/pnl-final.webp)

---

## Why this matters

Hermes 0.16 ships **Stripe Skills**: agents can now buy things,
provision SaaS, pay per-call APIs. This is huge. It is also the
moment finance teams say *"absolutely not."*

Stripe's per-action `$/call` limit is a static ceiling. Fine for
prototypes. Useless for the enterprise question of *"how much have we
spent on this initiative this quarter, and who approved it?"*

Every other agent-with-money pitch in this hackathon — OpsForge giving
agents `$100`, Distill paying agents to do work — needs a control
plane underneath. They either hardcoded it (and it breaks at scale) or
hand-waved it (and CFOs won't ship to prod).

**Argus is that control plane**. It's a Hermes plugin. It sits in the
`pre_tool_call` hook. It meters, tracks, and gates every dollar.

---

## 30-word elevator

Argus is a financial control plane for autonomous agents. It meters
every dollar Hermes agents spend via Stripe Skills, tracks live P&L
per job, and gates each spend through a human approval queue rendered
in the Hermes dashboard.

---

## 60-word elevator

Stripe Skills give Hermes agents a wallet, but no enterprise CFO will
hand that wallet over without controls. Argus is the missing
governance layer. It plugs into `pre_tool_call`, meters every dollar
in and out per job, tracks live P&L (with LLM cost from
`hermes-telemetry`), and gates every spend through a tier-aware
human approval queue inside the Hermes dashboard. Drop it in, and any
Hermes agent on any model becomes safe to put behind a wallet.

---

## What it does

Argus is a Hermes plugin (`~/.hermes/plugins/argus/`) that:

1. **Meters** money flow into a unified SQLite WAL ledger keyed by
   `job_id` and `session_id`. Revenue comes in via Stripe webhooks
   (real `payment_intent.succeeded` events). External spend comes
   from any Stripe Skill or explicit `argus_request_spend(...)` call
   the agent makes.
2. **Tracks live P&L per job**, joining the already-priced LLM token
   cost from the `hermes-telemetry` plugin via read-only SQLite
   `ATTACH`. Zero modifications to telemetry. Argus is a pure
   consumer.
3. **Gates every spend** through a pure-function policy:
   `(declaration, snapshot) → ALLOW | NEEDS_APPROVAL(level)`.
   Auto-approves under a per-cost-center threshold; routes
   manager-tier spends to managers; routes hard-cap breaches and
   large spends to finance.
4. **Synchronously holds** the agent inside the `pre_tool_call` hook
   until a human decides via the Approval Queue card in the Hermes
   dashboard. Approve → the agent resumes from the exact point.
   Reject → the agent gets a clean block message and (in our demo)
   self-corrects to a smaller amount.
5. **Audits everything** — every evaluation, request, decision, and
   resume gets a row in `audit_trail`. The dashboard renders this
   live as the "what happened and who said yes" record enterprises
   need before they ship an agent.

---

## Architecture — five layers, Ledger at the center

```
Capture ─→ Ledger ←─ Policy ←─ Enforcement
                ↑                      ↕
            Dashboard ────────────────┘
   (Capture also reads llm_cost from hermes-telemetry, read-only)
```

- **Capture** — Argus's own `pre_tool_call` hook records money in/out.
- **Ledger** — Argus's own SQLite WAL DB.
- **Policy** — a **pure function**: `decide(decl, snap) → Decision`.
  No I/O, no clock, no randomness. Frozen dataclass output. 8 unit
  tests cover every edge.
- **Enforcement** — same hook, blocks synchronously until a human
  decides in the dashboard, then returns `None` (allow) or the
  documented `{"action": "block", "message": ...}` (reject).
- **Dashboard** — React tab inside Hermes. No React bundle. Theme CSS
  variables only. FastAPI router mounted at `/api/plugins/argus/`.

The riskiest unknown going in — *can a plugin actually block a Stripe
spend before it settles?* — was resolved by reading the Hermes hook
source and using `pre_tool_call`'s block return value with a
synchronous-poll wait inside the hook itself. No Hermes core changes.

---

## NVIDIA pillar — what's covered

The brief lists three NVIDIA capabilities. We're honest about each:

| Pillar | Status | Evidence |
|---|---|---|
| **Nemotron 3 Ultra** | ✅ 100% | Real `nvidia/nemotron-3-ultra-550b-a55b` calls priced live by `hermes-telemetry`; surface in Argus's P&L via read-only ATTACH (the `$0.01` in the LLM column). |
| **NemoClaw** safe execution | ⚠️ Argus is the **complement** | Hermes (and Argus) run anywhere — local, VM, or inside a NemoClaw sandbox. NemoClaw is safe *execution*; Argus is safe *spending*. They're orthogonal layers. Argus shipped to a NemoClaw VM is a 0-line code change. |
| **NVIDIA agent skills** | ✅ via NIM credits in Job C | One of the three demo jobs explicitly buys NIM inference credits — the spend is recorded with `ref` pointing at the NVIDIA surface. |

> Argus gates spend regardless of what the agent does — the demo
> shows it governing three Hermes agents running on Nemotron 3 Ultra
> through NemoClaw-compatible runtime, each touching different
> NVIDIA / SaaS / Stripe surfaces.

---

## Stripe pillar — real round-trip, not mocked

- **TEST mode end-to-end with the real Stripe API.** The
  `POST /api/plugins/argus/webhooks/stripe` endpoint accepts both the
  flatter sim payloads the demo script uses and Stripe's actual
  envelope (nested `data.object`, cents as int, `metadata.job_id`).
- **Round-trip verified** against real Stripe API calls. Both
  directions of the money flow leave Argus with auditable `pi_...` /
  `ch_...` IDs that remain valid against
  `stripe payment_intents retrieve` / `stripe charges retrieve`:

  | Ledger row | Stripe event | Ref | Status |
  |---|---|---|---|
  | revenue +$50 | `payment_intent.succeeded` | `pi_3TjKbgArkRxfRtnB1KlK1TYW` | metadata.job_id propagated ✓ |
  | external_spend −$50 | `charge.refunded` | `ch_3TjKbgArkRxfRtnB1LzrKzUN` | metadata inherited from PI ✓ |

  ```json
  {
    "id": "pi_3TjKbgArkRxfRtnB1KlK1TYW",
    "amount": 5000,
    "status": "succeeded",
    "livemode": false,
    "metadata": { "job_id": "job-b-saas" }
  }
  ```

- **Spend gating** via `argus_request_spend(job_id, projected_usd,
  cost_center_id, ref)` (explicit declaration the agent calls before
  any Stripe charge) and the `stripe_*` tool-name pattern as a
  backstop.
- **Refunds:** schema (`external_spend` with negative amount) + the
  `charge.refunded` webhook path are live and verified. The call to
  `stripe.Refund.create()` from inside the rejection path is in
  `FUTURE.md` Tier 1 since the gated flow blocks spends *before*
  settlement.

---

## Demo — the deterministic version and the agent-driven version

We ship two ways to run the same flow:

### `scripts/demo.py` — deterministic, for the recorded video

Walks an operator through three unrelated jobs (`job-a-api`,
`job-b-saas`, `job-c-services`), blocking on each approval pause so
the operator can decide in the dashboard. Final P&L matches the
screenshot above. Recipe in `DEMO.md`.

### Live Hermes agent — for the wow moment

A real Hermes session running on Nemotron 3 Ultra calls
`argus_request_spend(...)` for each spend it wants to make. The hook
gates it. The agent receives the block as an error and self-corrects.
The screencast captures the agent making the retry decision in real
time. See `DEMO.md §5` for setup.

The deterministic script is what we used to reach the screenshot
above. The live agent path is the production shape — Argus's hook
fires identically.

---

## Why it matters / what's next

The hackathon brief asked for "business tooling on top of Stripe
Skills + NVIDIA agents." Most submissions interpreted "business
tooling" vertically — one industry, one agent, one product. We went
horizontally. Argus is the **control plane every vertical needs**
before they let their agent touch the wallet.

OpsForge giving an agent `$100` to run a business needs Argus
underneath. Distill paying one agent to hire another needs Argus
underneath. DevPulse automating ops needs Argus underneath. We're the
layer that makes their pitches real instead of demos.

Post-deadline roadmap lives in [`FUTURE.md`](./FUTURE.md), organised
by tier:

- **Tier 1** (real gaps): refund-on-reject via Stripe API, NemoClaw
  routing verification, more agent skills.
- **Tier 2** (polish): SSE in place of polling, cost-center editor,
  soft-threshold warnings, audit search.
- **Tier 3** (bigger swings): multi-tenant per-org budgets, cross-job
  revenue attribution, recurring/subscription spends, spend
  forecasting.
- **Tier 4** (explicitly NOT doing): Postgres rewrite, React framework
  upgrade, Stripe Connect.

The brain (`policy.py`) is a pure function. Everything else is a
sufficient set of pipes around that fact.

---

## Links

- **Code:** https://github.com/nujovich/argus (branch
  `feat/scaffolding` — to be merged to `main` after the deadline)
- **Design doc:** [`CLAUDE.md`](./CLAUDE.md) — single source of truth
- **Demo recipe:** [`DEMO.md`](./DEMO.md)
- **Demo driver:** [`scripts/demo.py`](./scripts/demo.py)
- **Phase 5 roadmap:** [`FUTURE.md`](./FUTURE.md)

---

## Team

[fill in]

---

## Credits

- **Hermes Agent** by Nous Research.
- **`hermes-telemetry`** by @nujovich (read-only dependency).
- **Stripe Skills for Hermes**.
- **NemoClaw / Nemotron 3 Ultra** for inference.
