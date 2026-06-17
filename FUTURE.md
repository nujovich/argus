# Argus — Phase 5 and beyond

> What's deliberately out of scope for the hackathon v1 (deadline
> 2026-06-30) and lives here instead of in code. Anything in this file
> is either documented with intent or actively being avoided to keep v1
> shippable. See `CLAUDE.md §10` for the scope-lock rule that points
> here.

---

## Tier 1 — adjacent surface, high ROI

These close real gaps a judge or early user might raise. They don't
require redesigning anything in `CLAUDE.md`.

### 1. Real-time refund-on-reject (Stripe API)

**Status:** ledger schema supports it (`external_spend` with negative
amount); the `charge.refunded` webhook path is in production already
(round-trip verified with real Stripe IDs — see `SUBMISSION.md`).
**Missing:** the call to `stripe.Refund.create(payment_intent=...)`
from inside the rejection branch of `hook.on_pre_tool_call`.

**Why deferred:** CLAUDE.md §3 explicitly calls this "rare in the
gated path" — when Argus blocks the spend *before* settlement, no
charge exists to refund. The Stripe-side path is the safety net for
the case where a `stripe_*` skill races past the hook (e.g. the agent
calls Stripe directly without going through `argus_request_spend`).

**Effort:** ~2h. Add `stripe.api_key = os.environ["STRIPE_API_KEY"]`,
wrap the rejection branch with a `stripe.Refund.create(...)` call
guarded by env var, log the refund id back into `audit_trail`.

### 2. Real agent driving the demo

**Status:** the deterministic `scripts/demo.py` simulates three agents
via curl. The hook path is identical to what a real agent would hit,
but no Hermes chat session actually invokes `argus_request_spend`
during the demo.

**Why deferred:** non-determinism is bad for recorded video; setup
overhead is high for what's essentially a re-skin of the same flow.

**Effort:** ~4-6h. Register `argus_request_spend` as a real Hermes
tool via the plugin SDK's `ctx.register_tool(...)`. Add a tiny skill
that wraps the `Stripe Skill → argus_request_spend → spend` pattern.
Prompt Nemotron on a goal like *"buy $5 of NIM credits for job X"*
and let the hook gate it live. Document the swap in `DEMO.md §5`.

### 3. NemoClaw routing verification

**Status:** Nemotron 3 Ultra 550B is configured (provider=`nvidia`,
key live) and produces real `cost_usd` in `hermes-telemetry` that
joins into Argus's P&L. **Unverified:** that the inference path is
NemoClaw's safe-execution wrapper, not the bare
`integrate.api.nvidia.com` endpoint.

**Why deferred:** investigation, not implementation. The NVIDIA
pillar of the hackathon brief calls out NemoClaw explicitly; we
should be able to point at logs or config that prove the routing.

**Effort:** ~1-2h. Probably one command (`hermes model --verbose`
or similar), confirm the route in NemoClaw's docs, snapshot the
config into `DEMO.md §4`.

---

## Tier 2 — niceties

Things that make Argus feel polished but don't change the v1 story.

### 4. SSE in place of polling

`dashboard/src/index.jsx` polls `/pnl`, `/approvals`, and `/audit`
every 1.5s. For a single operator it's fine; for a busy team you'd
swap to Server-Sent Events from `plugin_api.py`. Hermes's plugin SDK
exposes `fetchJSON` but not an SSE helper — easiest fallback is a
plain `EventSource` against the plugin's mounted route.

**Effort:** ~3h.

### 5. Cost-center YAML editor inside the dashboard

`cost_centers.yaml` is hot-loaded by `config.load_budgets()` on every
Policy snapshot. A small admin UI inside the Argus tab (read YAML,
diff-edit, write atomically with a backup) would let ops change
thresholds without leaving the dashboard.

**Effort:** ~4h.

### 6. Soft-threshold warnings

`Budget.soft_threshold` (default 0.8) is stored in YAML but not yet
consumed. Policy could emit a `SOFT_WARN` verdict (still ALLOW, but
the audit logs a warning + the dashboard tints the row).

**Effort:** ~1h. Pure addition to `policy.py` + audit + UI.

### 7. Audit trail filtering / search

Currently `GET /audit?limit=50` is FIFO. A real ops tool wants
`?event=approval_rejected`, `?actor=...`, `?since=2026-06-20T...`. Add
the params + indices on `(event)` and `(ts DESC)`.

**Effort:** ~2h.

### 8. Per-approval comments thread

Right now an approval has a single `reason` text field. For
enterprise audit, multiple comments (manager + finance back-and-forth)
matter. Schema would add an `approval_comments` table; UI shows them
chronologically.

**Effort:** ~3h.

---

## Tier 3 — bigger swings

Architectural extensions. Need their own design discussion before
landing.

### 9. Multi-tenant / per-org budgets

Today every plugin install governs a single Hermes instance. A SaaS
version of Argus would key all tables by `org_id`. Policy and routes
would scope. Auth needs a real session abstraction (Hermes's
`HERMES_DASHBOARD_SESSION_TOKEN` becomes one of many).

**Effort:** ~weeks. Different product.

### 10. Cross-job revenue attribution

The current "session → job → cost_center" chain attributes spends but
revenue is whatever the Stripe webhook says it is. Real businesses
have revenue that spans multiple jobs (e.g. a subscription paying for
N agent runs). Needs a `revenue_allocations` table to fan-out a
single Stripe payment across jobs.

**Effort:** ~1 week.

### 11. Recurring / scheduled spends

Stripe Skills can pay for SaaS via subscription. The agent provisions
once but the recurring charges land monthly. Argus would need:
- A `recurring_spend_authorizations` table mapping
  `(job_id, cost_center_id, monthly_cap_usd, expires_at)`.
- A webhook handler for `invoice.paid` that decrements the cap.
- Policy aware that "this is an already-authorized recurring spend,
  don't re-gate."

**Effort:** ~1 week.

### 12. Spend forecasting

Given the per-job ledger, predict whether a job will breach budget
before completion. Simple: linear extrapolation of spend rate. Better:
LLM-judged based on session transcript. Surfaces in the dashboard as
"⚠ job-b-saas is on track to spend $250 by tomorrow."

**Effort:** ~3-5 days for a credible v0.

---

## Tier 4 — explicitly NOT doing

Things considered and rejected.

### 13. Native Hermes "park + resume" primitive

CLAUDE.md §6 documents this: Hermes has no async park/resume; we use
a synchronous-poll inside `pre_tool_call`. If a future Hermes release
adds richer hooks, we'd swap our impl behind the same Policy →
Enforcement interface. Not on our roadmap — on theirs.

### 14. React framework upgrade

The plugin reads React from `window.__HERMES_PLUGIN_SDK__` and never
bundles it. Tempting to add Next.js / Tanstack-Query / Zustand. CLAUDE.md
§7 forbids it. Polling + raw fetch is the right level for this scope.

### 15. Replace SQLite with Postgres

WAL SQLite handles thousands of approvals/sec and gives us the
read-only `ATTACH` to `hermes-telemetry` for free. Postgres only
matters at multi-tenant scale (see #9), and at that point the whole
plugin needs to become a service.

### 16. Real Stripe Connect / direct charges

Out of scope. Argus governs spend; it doesn't process payments.
Stripe Skills (the part Hermes already ships) is the surface we
gate.

---

## How to use this file

When a new idea comes up:

1. Does it serve `usefulness / viability / presentation` for the
   hackathon deadline? If yes → it goes in code, update CLAUDE.md if
   needed.
2. If no → append it here under the right tier with: short summary,
   why deferred, rough effort.

This file is the single place ideas don't vanish but also don't bloat
v1. The roadmap reads top-to-bottom.
