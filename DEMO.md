# Argus — demo walkthrough

> Stripe gives agents a wallet; Argus puts a hundred eyes on it.

This file is the reproducible recipe for the hackathon demo. Anyone who
can follow it will reach the same final P&L screen and the same audit
trail in under three minutes.

The demo proves Argus's thesis — **horizontal financial control for
money-spending agents** — by governing three unrelated jobs with the
*same* policy + ledger + approval queue:

| Job | Cost center | What the agent does | Tier hit |
|---|---|---|---|
| A | `api_calls` | 5 micro-charges + 1 batch burst | auto × 5, then **manager** |
| B | `saas` | Provisions a $79 Postgres tier | **finance** |
| C | `services` | Buys $7 of NIM credits, gets rejected, retries with $3 | **manager** × 2 |

---

## 1. Setup (one-time per machine)

```bash
# 1.1 Clone the repo
git clone https://github.com/nujovich/argus.git ~/argus
cd ~/argus && git checkout feat/scaffolding

# 1.2 Build the dashboard bundle
npm install
npm run build

# 1.3 Install the FastAPI deps into Hermes's own Python
~/.hermes/hermes-agent/venv/bin/python -m pip install fastapi pydantic pyyaml anyio

# 1.4 Symlink into ~/.hermes/plugins so Hermes discovers it
ln -sf ~/argus ~/.hermes/plugins/argus

# 1.5 Drop the demo cost-center config into the Argus home dir
mkdir -p ~/.hermes/argus
cp ~/argus/cost_centers.yaml ~/.hermes/argus/cost_centers.yaml
```

Sanity check:

```bash
ls -la ~/.hermes/plugins/argus/dashboard/manifest.json   # symlink target exists
ls -la ~/.hermes/argus/cost_centers.yaml                 # config in place
~/.hermes/hermes-agent/venv/bin/python -c "import fastapi, pydantic, yaml, anyio; print('deps ok')"
```

---

## 2. Run the demo

### 2.1 Start the dashboard

In **terminal #1** (this stays open for the whole demo):

```bash
# Kill anything still bound to the port
pkill -f hermes; sleep 2

# Use a known auth token so the script can reach the API
export HERMES_DASHBOARD_SESSION_TOKEN=argus-demo

hermes dashboard --port 9119 --no-open
```

Open the browser at `http://127.0.0.1:9119/argus`. You should see three
empty cards: **P&L per job**, **Approval queue (0 pending)**, **Audit
trail**.

### 2.2 Run the driver

In **terminal #2**:

```bash
cd ~/argus
export HERMES_DASHBOARD_SESSION_TOKEN=argus-demo
python3 scripts/demo.py
```

The script blocks at every spend that needs human approval. Each time
it pauses, the dashboard's **Approval queue** lights up with a card.
Click **Approve** or **Reject** per the script's prompt:

| Script prompts | Your move | Why |
|---|---|---|
| Job A — batch_backfill $8.00 → MANAGER | **Approve** | Show the resume path |
| Job B — postgres_tier_3yr $79.00 → FINANCE | **Approve** | The climactic beat |
| Job C — nim_credits_first $7.00 → MANAGER | **Reject** | Show the block + agent self-correct |
| Job C — nim_credits_retry $3.00 → MANAGER | **Approve** | Show recovery |

The script prints the final P&L when it's done.

### 2.3 What the dashboard shows

After the script completes you should see roughly:

```
Job              Revenue     LLM     External   P&L
job-a-api        $25.00     $0.00      $8.10   +$16.90
job-b-saas      $120.00     $0.00     $79.00   +$41.00
job-c-services    $9.00     $0.00      $3.00    +$6.00
─────────────────────────────────────────────────────
TOTAL           $154.00     $0.00     $90.10   +$63.90
```

(`LLM` lights up once a real Hermes agent runs on Nemotron — see §4.)

The **Audit trail** card has the full chain for every spend:
`spend_evaluated → approval_requested → approval_approved → spend_resumed`
for the approves, and the analogous `_rejected` chain for Job C's first
attempt.

---

## 3. The 60-second pitch (for the video)

> *"Stripe gives agents a wallet — but no enterprise CFO is going to
> hand that wallet to an autonomous agent without controls. That's
> Argus. It sits on top of Hermes + Stripe Skills as a horizontal
> financial control plane: it meters every dollar in and out per job,
> tracks live P&L, and gates every spend through a policy that knows
> the cost-center budget. Small spends auto-approve. Medium ones
> route to a manager. Large ones — like this $79 SaaS purchase
> happening right now — wait for finance. Three completely
> unrelated jobs — a pay-per-call API, a SaaS provisioning agent,
> and a service buyer — governed by the same control layer. One
> ledger, one audit trail, one queue. Stripe gives agents a
> wallet; Argus puts a hundred eyes on it."*

---

## 4. NVIDIA pillar — the bits that earn the NVIDIA half of the brief

Argus's code is model-agnostic, so NVIDIA only counts if the **demo
wiring** uses it. Three deliverables, none of which require Argus code
changes — they're all Hermes configuration on the demo machine.

### 4.1 Run the demo agent(s) on Nemotron 3 Ultra via NemoClaw

```bash
# Configure Hermes to use Nemotron 3 Ultra through NemoClaw
hermes model
# In the menu: pick provider → NemoClaw; model → nemotron-3-ultra
# (Or set the equivalent NEMOCLAW_API_KEY / HERMES_DEFAULT_MODEL env vars.)
```

When the demo runs **with a live Hermes agent** instead of the
deterministic script (see §5), the `LLM cost` column in P&L is now
specifically Nemotron-priced — surfaced by Argus's read-only ATTACH to
`hermes-telemetry`.

### 4.2 At least one NVIDIA-surface spend

In the demo as-shipped, **Job C** is named explicitly to buy *NIM
inference credits*. That's an NVIDIA paid surface. The `ref` field in
the ledger row (`nim_credits_retry`) is the auditable hook to point
the judges at:

```bash
sqlite3 ~/.hermes/argus/argus.db \
  "SELECT job_id, amount_usd, ref FROM ledger WHERE ref LIKE 'nim_%';"
```

To make it spend on a *real* NIM endpoint, swap the simulated
`sim/spend` call for a Hermes skill that hits a NIM. The Argus
gating path is identical.

### 4.3 Writeup line (for the submission form)

> *"Argus gates spend regardless of what the agent does — the demo
> shows it governing three Hermes agents running on Nemotron 3 Ultra
> through NemoClaw, each touching different NVIDIA / SaaS / Stripe
> surfaces."*

---

## 5. Optional: drive the demo with a live Hermes agent

The shipped script in §2 is deterministic — perfect for a recorded
video where you don't want LLM non-determinism. To run the demo with a
real Hermes agent making decisions instead:

1. Configure a chat profile that has access to a `request_spend(job_id,
   projected_usd, cost_center_id, ref)` skill and the relevant Stripe
   skill(s). The skill must call `argus_request_spend(...)` first; the
   hook will gate it.
2. Prompt the agent with a goal like *"buy $5 of NIM credits for job
   X"*. The pre_tool_call hook intercepts the spend just like in the
   script and waits for the dashboard.
3. Approve or reject — the agent resumes (or self-corrects on block)
   in real time.

Trade-off: less reproducible, more impressive. If your screencast
budget allows, do both — script for the bulk, one live agent moment
for the wow.

---

## 5b. Optional: drive revenue from real Stripe (TEST mode)

Same payload shape as the demo sim, but emitted by Stripe itself. Useful
when you want to show the judges that the wiring is real, not a hand-
written JSON.

### Install + login (one-time)

```bash
# Install Stripe CLI (Linux/WSL): https://docs.stripe.com/stripe-cli
curl -s https://packages.stripe.dev/api/security/keypair/stripe-cli-gpg/public \
  | gpg --dearmor | sudo tee /usr/share/keyrings/stripe.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/stripe.gpg] https://packages.stripe.dev/stripe-cli-debian-local stable main" \
  | sudo tee /etc/apt/sources.list.d/stripe.list
sudo apt update && sudo apt install stripe

# Sign in to your Stripe account (TEST mode is the default)
stripe login
```

### Forward webhooks to Argus (terminal #3)

```bash
stripe listen \
  --forward-to http://127.0.0.1:9119/api/plugins/argus/webhooks/stripe \
  --skip-verify    # we don't verify the signature in v1
```

Leave this running. Stripe CLI is now a tunnel; any event in your TEST
account hits Argus.

### Trigger a real payment_intent.succeeded

```bash
# Attach the demo's job_id via metadata so Argus's ledger ties revenue
# to the right job in the P&L view
stripe trigger payment_intent.succeeded \
  --add payment_intent:metadata.job_id=job-b-saas \
  --add payment_intent:amount=12000
```

Refresh the Argus tab. The audit trail should show a fresh
`revenue_received` row for `job-b-saas` of `$120.00`, and the P&L
column updates. The `ref` field in the ledger holds the real
`pi_...` id — clickable in your Stripe dashboard.

### Trigger a refund

```bash
stripe trigger charge.refunded \
  --add charge:metadata.job_id=job-b-saas \
  --add charge:amount_refunded=5000
```

Argus writes a `-$50.00` `external_spend` row → P&L recalculates.

---

## 6. Reset between takes

```bash
# Wipe Argus's own ledger (telemetry stays untouched)
rm -f ~/.hermes/argus/argus.db
```

Next request to the plugin recreates the schema fresh.

---

## 7. Troubleshooting

- **`Unauthorized`** on every curl → the dashboard wasn't started with
  `HERMES_DASHBOARD_SESSION_TOKEN`; restart it in a shell where the
  env var is exported.
- **`No such API endpoint: /api/plugins/argus/...`** → the plugin
  isn't loaded. Check `~/.hermes/plugins/argus` symlink and
  `~/.hermes/hermes-agent/venv/bin/python -c "import fastapi"`.
- **`address already in use`** → previous dashboard is still bound to
  9119. `ss -tlnp | grep 9119` → kill the PID.
- **P&L `500 Internal Server Error`** → likely an old build before the
  ATTACH-URI fix. `git pull` and restart the dashboard.
- **Argus tab doesn't appear** → `HERMES_HOME` is pointing somewhere
  the symlink doesn't exist. `unset HERMES_HOME` and restart.
