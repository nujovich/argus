---
name: argus-request-compute
description: |
  REQUIRED before starting any LLM-heavy job. Declares (expected_revenue,
  projected_burn) so Argus can assign a Nemotron tier — Ultra for
  high-margin work, Base for thin-margin work, REJECT for negative-margin
  jobs. The response tells you which model to use for this job.
trigger: |
  Call this skill BEFORE running an LLM-heavy operation (deep research,
  multi-turn generation, long reasoning chains). Argus allocates compute
  as capital: premium jobs get Ultra, low-margin jobs get downgraded.
---

# Argus Request Compute

## Why this exists

You (the agent) burn money every time you call an LLM. The customer is
paying you a fixed price; your margin is whatever's left after compute.
Argus is the **economic OS** of this AI factory. Before you spin up a
big inference job, you declare what you expect to earn and what you
expect to burn. Argus tells you which Nemotron tier you're authorized
to run on:

- **Ultra** — `nvidia/nemotron-3-ultra-550b-a55b`, premium, expensive.
  Justified only when revenue × margin is high enough.
- **Base** — a cheaper Nemotron tier. Argus defaults here for
  thin-margin work.
- **Reject** — if Argus computes negative margin, the job is killed
  before it burns a dollar.

## The contract

Before any expensive inference, hit the Argus compute endpoint:

```bash
curl -s \
  -H "Authorization: Bearer ${HERMES_DASHBOARD_SESSION_TOKEN:-argus-demo}" \
  -H "content-type: application/json" \
  -X POST http://127.0.0.1:9119/api/plugins/argus/sim/compute \
  -d '{
    "job_id":               "<the job this work belongs to>",
    "cost_center_id":       "<one of: ai_research, ai_generation, default>",
    "expected_revenue_usd": <what you expect to earn from this job>,
    "projected_burn_usd":   <what you expect to burn on Nemotron tokens>,
    "ref":                  "<a short human-readable label>",
    "session_id":           "<your task_id / Hermes session id>"
  }'
```

Argus responds with:

| Response (`result.action`) | What it means | What you do |
|---|---|---|
| `allow` with `tier: "ultra"` | High-margin job → Ultra approved | Run inference on `result.model` (Nemotron 3 Ultra), echo `result.auth_token` in `metadata.argus_auth_token` |
| `allow` with `tier: "base"` | Margin too thin for Ultra → Base assigned | Run on `result.model` (cheaper Nemotron), same token convention |
| `block` with `verdict: "TIER_REJECT"` | Argus computes negative margin → job killed | Don't run. Report back: "margin would be negative, the job is uneconomic." |
| `block` with `compute_*` reason | Monthly cap or other policy block | Self-correct (lower projected burn) or escalate |

## Example — high-margin enterprise research

```bash
curl -s -H "..." -X POST http://127.0.0.1:9119/api/plugins/argus/sim/compute \
  -d '{
    "job_id":"research-enterprise-001",
    "cost_center_id":"ai_research",
    "expected_revenue_usd":200,
    "projected_burn_usd":15,
    "ref":"deep_competitor_analysis"
  }'
```

Response (abbreviated):

```json
{ "result": {
    "action": "allow",
    "verdict": "TIER_ULTRA",
    "tier": "ultra",
    "model": "nvidia/nemotron-3-ultra-550b-a55b",
    "compute_budget_usd": 15.0,
    "expected_margin_usd": 185.0,
    "auth_token": "abc123…"
} }
```

You now run your research on `nvidia/nemotron-3-ultra-550b-a55b` and
deliver to the customer.

## Example — low-margin generation, downgraded

```bash
curl -s -H "..." -X POST http://127.0.0.1:9119/api/plugins/argus/sim/compute \
  -d '{
    "job_id":"gen-tweet-042",
    "cost_center_id":"ai_generation",
    "expected_revenue_usd":3,
    "projected_burn_usd":5,
    "ref":"viral_tweet_draft"
  }'
```

Response:

```json
{ "result": {
    "action": "block",
    "verdict": "TIER_REJECT",
    "reason": "negative_margin: expected_margin -2.00 < threshold 0.00"
} }
```

Or if the policy allows base for a barely-positive job:

```json
{ "result": {
    "action": "allow",
    "verdict": "TIER_BASE",
    "tier": "base",
    "model": "nvidia/nemotron-3-base-9b",
    "compute_budget_usd": 0.5,
    "expected_margin_usd": 0.5
} }
```

Either way: **don't burn Ultra inference on a $3 job**.

## Why this is non-negotiable

You're not running on a free tier. Every Nemotron token costs your
employer real dollars. Argus is the layer that makes the
economics of agentic work visible — and survivable — at scale. Use
this skill before every LLM-heavy job. The audit trail records your
choice; future humans (and CFOs) will read it.
