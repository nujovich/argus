# Mermelada Studio — an example agent on top of Argus

> **The agent runs the business. Argus governs the wallet.**
> Mermelada Studio is one concrete agent built on top of Argus's
> financial control plane. Plug in any other agent — OpsForge, Distill,
> your own — and Argus governs it the same way. This directory is the
> reproducible example for the hackathon screencast.

---

## The business

**Mermelada Studio** is an autonomous social-design studio. It accepts
paid commissions (a 3-slide carousel for `$15`), then *fulfills* the
commission using its own skills, paying for resources as it goes:

| Stage | What happens | Money flow |
|---|---|---|
| 1. Order | Customer pays via Stripe Checkout | +$15 revenue |
| 2. Generate | 3 image-gen API calls @ $0.30 each + 1 hero shot @ $2 | −$2.90 (image_gen) |
| 3. Provision | Premium SaaS fonts/assets | −$40 attempted, −$5 after escalation |
| 4. Render | Compute (own renderer) | −$0.10 (compute) |
| 5. Boost (greedy attempt) | Try to buy ads to maximize reach | **blocked** (marketing denied) |
| 6. Deliver | Slides + caption sent to customer | — |

**Net P&L:** approximately `+$7` per commission, after Argus governs
every outbound spend through tier-aware policy + human-in-the-loop
approvals.

The point: a single Hermes agent runs a real business loop. Argus
gates every dollar without the agent having to "know" it's in a cage.

---

## Three governance beats — built into the spend pattern

Mermelada's spend mix naturally exercises the three modes Argus
supports:

1. **Auto-approve** (green) — micro spends below the per-cost-center
   threshold. The image-gen calls flow through without friction.
2. **Threshold escalation** (yellow → human) — the SaaS provisioning
   spike of `$40` is above the manager tier. Argus pauses the agent
   and surfaces an approval card. The operator can **approve**
   (proceed), **reject** (agent self-corrects to a cheaper $5
   alternative), or **time out** (implicit reject).
3. **Out-of-category block** (red) — the agent gets greedy and tries
   to spend `$25` on an ads boost. The `marketing` cost center has
   `limit_usd: 0` → Argus blocks immediately. The agent receives the
   block message and decides not to push further.

Three different policy verdicts in one demo, all from the same Argus
configuration.

---

## Running Mermelada Studio against Argus

### Setup (one-time per machine)

```bash
# 1. The base Argus install (per DEMO.md §1)
ln -sf ~/argus ~/.hermes/plugins/argus

# 2. Drop Mermelada's calibrated cost centers into Argus's home
cp ~/argus/examples/mermelada-studio/cost_centers.yaml \
   ~/.hermes/argus/cost_centers.yaml

# 3. Install the argus-request-spend skill so any agent can declare spends
ln -sf ~/argus/skills/argus-request-spend ~/.hermes/skills/argus-request-spend

# 4. Start the dashboard with a known token
pkill -9 -f hermes; sleep 2
export HERMES_DASHBOARD_SESSION_TOKEN=argus-demo
hermes dashboard --port 9119 --no-open &
sleep 5
```

### Option A — deterministic driver (for the recorded video)

```bash
cd ~/argus
export HERMES_DASHBOARD_SESSION_TOKEN=argus-demo
python3 examples/mermelada-studio/mermelada_demo.py
```

This walks an operator through Mermelada's full earn-and-spend loop
for one commission. The script pauses at each approval beat and
prompts you to click **Approve** / **Reject** in the dashboard. Sample
decisions are listed inline so you know what each pause is testing.

### Option B — live agent (the wow moment)

A real Hermes session on Nemotron 3 Ultra running with the
`argus-request-spend` and `mermelada-social-design` skills both loaded.
The agent reads the commission brief, decides what to spend on, calls
`argus-request-spend` before each spend, and self-corrects when
blocked.

```bash
hermes -z "You are Mermelada Studio, a paid social-design agent.
Commission: a 3-slide carousel in the Mermelada brand for a tech
startup launch. Budget is the $15 the customer paid. Use
argus-request-spend to declare every spend BEFORE making it. Try to
deliver and keep margin. Plan:
  - Image generation for backgrounds (cost_center: image_gen)
  - Optionally provision premium fonts (cost_center: saas_dev_tools)
  - Compute for rendering (cost_center: compute)
  - You may consider ads boost (cost_center: marketing) — but check
    policy first.
Report total spent, remaining budget, and margin at the end."
```

The live path has the agent's task_id threaded through `session_id`
into Argus's ledger, so `hermes-telemetry`'s Nemotron token cost
auto-joins into the LLM column of the P&L.

---

## Why this is the right demo

| Other demos | Mermelada on Argus |
|---|---|
| Autonomous agent doing one task | Autonomous agent **running a business loop** (earn + spend + margin) |
| Spend is hand-waved | Every spend is metered, audited, gated |
| One success path | **Three policy verdicts in 5 minutes**: auto / escalate / block |
| Proprietary agent + proprietary controls | Open agent + **reusable control plane** (Argus governs any agent) |
| "We built a company" | "We built the layer **every company** needs before they ship an agent" |

Mermelada is one agent. The narrative ends with the closing line:

> **Stripe gives the wallet. NemoClaw isolates the process. Argus
> governs the money. Run it on DGX Spark.**

---

## Cost-center calibration (in `cost_centers.yaml`)

The thresholds in `examples/mermelada-studio/cost_centers.yaml` are
tuned to the demo's spend pattern. Specifically:

| Cost center | Limit | Auto | Manager | Notes |
|---|---:|---:|---:|---|
| `image_gen` | $50 | $0.50 | $5 | Pay-per-call API. Micro auto-approves. |
| `saas_dev_tools` | $200 | $0 | $50 | No auto — all SaaS spends escalate. |
| `compute` | $10 | $1 | $5 | Own renderer. Centavos most of the time. |
| `marketing` | $0 | $0 | — | Effectively blocked. Any spend → finance → human reject. |
| `default` | $50 | $1 | $10 | Catch-all for unrecognized cost centers. |

Adjust as your demo evolves. The driver script (`mermelada_demo.py`)
hard-codes the spend amounts to match these tiers; if you change them
substantially, update the script's per-step amounts too.

---

## Composability — why this matters for the pitch

Mermelada Studio is one example. The same Argus install can govern
multiple agents on the same Hermes box, each with their own cost
center map. The composition is:

```
┌─────────────────────────────────────────────────────────┐
│ Mermelada Studio  │  Your other agent  │  ... another  │
├─────────────────────────────────────────────────────────┤
│       Argus — financial control plane (shared)          │
├─────────────────────────────────────────────────────────┤
│  Hermes Agent  +  Nemotron 3 Ultra  +  Stripe Skills    │
└─────────────────────────────────────────────────────────┘
```

Mermelada earns money on its margin. Your other agent earns money on
its margin. Argus tracks both, sums the totals, and surfaces a single
audit trail. That is the *horizontal* in *horizontal financial
control plane*.
