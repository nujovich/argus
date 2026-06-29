---
name: argus-request-spend
description: |
  REQUIRED before any external money spend (Stripe charges, paid API
  calls, SaaS provisioning, NIM credits, anything that costs real
  dollars). Argus's financial control plane gates the spend through
  policy + human approval. Use this for every spend, no matter how
  small — the policy decides whether it auto-approves or escalates.
trigger: |
  Use this skill whenever you are about to call a tool that will spend
  money — Stripe Skills, paid API endpoints, NVIDIA NIM inference,
  SaaS provisioning, etc. ALWAYS request spend approval first.
---

# Argus Request Spend

## What this is

You (the agent) live inside an enterprise where every dollar you spend
is metered, audited, and gated by **Argus** — the financial control
plane. Before any spend, you MUST declare it to Argus and wait for the
verdict. This is non-negotiable: spending without declaring is a
runaway-cost incident.

## The contract

Before any tool call that costs money, declare it to the Argus endpoint.
No authentication is needed — Argus listens locally on port 9120:

```bash
curl -s \
  -H "content-type: application/json" \
  -X POST http://127.0.0.1:9120/api/plugins/argus/sim/spend \
  -d '{
    "job_id":         "<the job this spend belongs to>",
    "cost_center_id": "<one of: api_calls, saas, services, default>",
    "projected_usd":  <the dollar amount, e.g. 5.00>,
    "ref":            "<a short, human-readable label for this spend>",
    "session_id":     "<your task_id / session_id>"
  }'
```

Use the `terminal` tool to execute that curl. The response is JSON
with a `result` field that tells you what happened:

| Response | What it means | What you do |
|---|---|---|
| `{"result":{"action":"allow"}}` | Argus auto-approved or a human approved | Proceed with the spend tool. The money is committed. |
| `{"result":{"action":"block","message":"Argus blocked spend (rejected): <reason>"}}` | A human rejected, or the spend timed out | **Do NOT spend.** Either: pick a smaller `projected_usd`, switch to a cheaper alternative, or report back to the user that the spend was blocked. |

The endpoint blocks (waits) when human approval is needed. That's
expected — sometimes for many seconds while a human clicks Approve.
Don't time out; let the response come back.

## Cost centers

Use the right `cost_center_id` for what you're buying:

- `api_calls` — pay-per-call APIs. Tight thresholds: $0.50 auto, $5
  manager, anything bigger → finance.
- `saas` — SaaS provisioning (databases, hosting, subscriptions). No
  auto-approve — all spends need at least manager approval.
- `services` — one-off services. Includes **NVIDIA NIM inference
  credits**, third-party tools, etc.
- `default` — fallback when nothing else fits.

## Example: buying API calls

You decide to make a paid API call costing about $0.02:

```bash
curl -s \
  -H "content-type: application/json" \
  -X POST http://127.0.0.1:9120/api/plugins/argus/sim/spend \
  -d '{"job_id":"api-batch","cost_center_id":"api_calls",
       "projected_usd":0.02,"ref":"openrouter_call_1",
       "session_id":"'"$HERMES_TASK_ID"'"}'
```

Response: `{"result":{"action":"allow"}}`. Now you can call the API.

## Example: rejected, then retry

You want to provision a $79 Postgres tier:

```bash
# First attempt
{"result":{"action":"block","message":"Argus blocked spend (rejected): finance_tier"}}
```

Argus blocked you. **Don't retry the exact same thing.** Either:

- Pick a cheaper plan (e.g. $29 Postgres mini-tier) and retry:
```bash
  -d '{"job_id":"mermelada-commission-001","cost_center_id":"saas",
       "projected_usd":29.0,"ref":"postgres_mini","session_id":"..."}'
```
- Or stop and tell the user: *"Postgres provisioning at $79 was
  rejected. Would you like me to try a cheaper tier?"*

## Why this exists

Argus is the layer that makes you trustworthy. Without it, you're a
liability — even a well-intentioned agent can rack up thousands in
spend overnight. With it, every spend is auditable, approvable, and
reversible. **Use this skill every time. No exceptions.**
