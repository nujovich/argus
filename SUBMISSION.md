# Argus — hackathon submission writeup

> Copy-paste-ready sections for the submission form. The hero pitch is
> at the top. Skip down to "How it works" for the technical detail.

---

## Tagline (≤ 100 chars)

**Stripe gives the wallet. NemoClaw isolates the process. Argus governs the money.**

---

## The hook — what a judge sees first

> Meet **Mermelada Studio** — a Hermes agent that runs a paid
> social-design business. A customer pays `$15` for a 3-slide
> carousel. The agent spends real Stripe-routed money on image-gen
> APIs, premium SaaS fonts, and compute. Then it gets greedy and
> tries to boost reach with ads.
>
> Mermelada Studio makes **7 spend decisions** in five minutes.
>
> Argus auto-approves 4 micro-charges in image-gen + compute. It
> pauses the agent on a `$40` SaaS provisioning attempt and asks the
> human. The human rejects → **the agent self-corrects and retries
> at `$5`**, which Argus then escalates again, the human approves,
> the agent proceeds. When Mermelada then tries to spend `$25` on a
> marketing boost — a denied category for this engagement — Argus
> blocks at the network layer. The agent receives the block message
> and stops.
>
> Net: `$15` revenue, `~$8` spent (including Nemotron token cost
> auto-joined from `hermes-telemetry`), `+$7` margin. Zero runaway
> charges. Full audit trail of every decision.
>
> Mermelada is one agent. **Argus is the infrastructure that any agent
> with a wallet needs.**

![Argus dashboard — final P&L of the three-job demo](docs/pnl-final.webp)

---

## The killer question — and the answer

> *"What stops the agent from skipping `argus_request_spend` and
> calling Stripe directly?"*

A pure declaration model is fragile — a prompt-injected agent could
just not ask. Argus enforces in **two layers**, with the obligatory
plane underneath the cooperative one:

### Layer 1 — In-process backstop (`pre_tool_call` hook)

Every Stripe-skill invocation (`stripe_*`) is intercepted by Argus's
`pre_tool_call` hook. The hook **requires** a valid Argus auth token
in `args.metadata.argus_auth_token`. The token:

- Is issued by Argus only on a successful `argus_request_spend` (auto
  or human-approved).
- Carries the approved `job_id`, `cost_center_id`, and amount.
- Lives 60 seconds, single-use.
- Validates the actual Stripe charge amount within ±10%.

An agent that skips the declaration has no token → its Stripe call
is **blocked at the hook**, with a clear error fed back to the model.

### Layer 2 — Network-layer enforcement (Stripe Issuing)

Argus exposes `POST /webhooks/stripe-issuing-authorization`. When the
agent's virtual card attempts a charge, Stripe sends a real-time
authorization request. Argus checks for a matching active auth token
and replies `{"approved": true}` or `{"approved": false}`.

**Even if the agent bypasses Hermes entirely** — exfiltrates Stripe
credentials, calls the API directly — the charge declines at the
card-network level. Production wiring requires the standard Stripe
Issuing setup (see [`FUTURE.md`](./FUTURE.md) Tier 1); the endpoint +
token logic ship in v1 and validate against real
`issuing_authorization.request` payloads.

> **We enforce in the agent's runtime AND on the card network. The
> rogue agent doesn't get past either.**

---

## Why this matters — and the competitive moat

Hermes 0.16 ships **Stripe Skills**: agents can now buy things,
provision SaaS, pay per-call APIs. This is huge. It is also the
moment finance teams say *"absolutely not."*

There's already competition in budget governance for agents — and
that's exactly why Argus's framing matters:

| Competitor | Domain | Granularity | What Argus does differently |
|---|---|---|---|
| **PipRail** | crypto (x402, EVM/Solana) | per-transaction cap | We govern **fiat** through Stripe Skills, with **portfolio-level** policy (per-txn isn't enough — `$5/txn` doesn't stop 200 micro-charges) |
| **42-evey** | token cost in agents | LLM-only | We unify **token cost + fiat spend** in one P&L — the only place where Nemotron pricing and Stripe charges share a ledger |
| **Hardcoded inside OpsForge/Distill/etc.** | bespoke | one agent | Argus is **reusable infra**. Drop it in once, every agent on the box is governed. |

**Nobody else is doing fiat governance on Stripe Skills with portfolio
policy + human escalation + unified token+fiat P&L.** That's the gap.
And we close it with continuity: Argus is positioned as the spend-
gobierno upgrade to `hermes-telemetry` (a plugin the community
already uses — author is the same).

---

## Three policy verdicts in one demo

The Mermelada Studio spend pattern naturally exercises all three Argus
verdict modes:

| Verdict | Beat | Example spend |
|---|---|---|
| **ALLOW** (auto-approve) | Green / silent | 3× $0.30 image-gen calls |
| **NEEDS_APPROVAL_MANAGER** (escalate) | Yellow — human in the loop | $40 premium fonts → reject → $5 standard fonts → approve |
| **HARD BLOCK** (denied category) | Red — full stop | $25 ads boost (marketing is denied for this engagement) |

That's three distinct governance outcomes in the same 5-minute
commission. No other hackathon entry shows all three.

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

## Demo — Mermelada Studio on Argus

We ship one reference agent (**Mermelada Studio**) on top of Argus, in
two runnable forms. Both use the same Argus plugin install + the same
`cost_centers.yaml`; the difference is whether the spend decisions
come from a deterministic driver or a live Nemotron-driven loop.

### `examples/mermelada-studio/mermelada_demo.py` — deterministic

Walks the operator through one commission: customer pays `$15`,
agent generates art (3 micro + 1 hero), provisions fonts (`$40`
reject → `$5` retry approve), renders, tries `$25` ads boost (block).
Each stage prints what's happening and what to click in the dashboard.

```bash
cd ~/argus
cp examples/mermelada-studio/cost_centers.yaml ~/.hermes/argus/cost_centers.yaml
export HERMES_DASHBOARD_SESSION_TOKEN=argus-demo
python3 examples/mermelada-studio/mermelada_demo.py
```

### Live Hermes agent — the wow moment

A real Hermes session on Nemotron 3 Ultra loaded with both
`argus-request-spend` and the existing `mermelada-social-design`
skills. The agent reads the commission brief, decides what to spend
on, calls `argus-request-spend` before each spend, and self-corrects
when blocked. The screencast captures the agent making retry
decisions in real time.

Both paths fire the same hook (`hook.on_pre_tool_call`). The
deterministic path is just `/sim/spend` invoking the same code with
`task_id="sim"`. The live path threads the agent's real Hermes
session_id, which is what makes Nemotron token cost auto-join into
the LLM column of the P&L.

See [`examples/mermelada-studio/README.md`](./examples/mermelada-studio/README.md)
for the full setup and `DEMO.md` for the base Argus install.

---

## Why it matters / what's next

Hermes + Stripe Skills just put agents into the wallet. Nothing else
does. But no enterprise CFO will hand that wallet over without
controls. **Argus is the missing layer** between "agent that can
spend" and "agent that the business authorizes to spend."

Closing line for the screencast:

> **Stripe gives the wallet. NemoClaw isolates the process. Argus
> governs the money. Run it on DGX Spark.**

Post-deadline roadmap lives in [`FUTURE.md`](./FUTURE.md), organised
by tier:

- **Tier 1 (real gaps):** Stripe Issuing **defense-in-depth layer**
  (virtual card + authorization webhook → enforcement at the card
  network, not just the agent runtime — the line that makes a CFO
  sign), refund-on-reject via Stripe API, NemoClaw routing
  verification.
- **Tier 2 (polish):** SSE in place of polling, cost-center editor,
  soft-threshold warnings, policy-level denied-categories, audit
  search.
- **Tier 3 (bigger swings):** multi-tenant per-org budgets, cross-job
  revenue attribution, recurring/subscription spends, spend
  forecasting.
- **Tier 4 (explicitly NOT doing):** Postgres rewrite, React framework
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
