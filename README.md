# Argus

> Stripe gives agents a wallet; Argus puts a hundred eyes on it.

Horizontal financial control plane for money-spending Hermes agents.
Meters every dollar in/out per job, tracks live P&L, and gates Stripe
spends through a human-in-the-loop approval flow.

![Argus dashboard — final P&L of the three-job demo](docs/pnl-final.webp)

**Status:** Phase 4 — demo ready. Ledger, pure policy, `pre_tool_call`
Capture + Enforcement hook, dashboard (P&L, approval queue, audit
trail), and a three-job deterministic demo driver are all live. See
[`DEMO.md`](./DEMO.md) for the reproducible recipe; full design in
[`CLAUDE.md`](./CLAUDE.md).

> Argus gates spend regardless of what the agent does — the demo shows
> it governing three Hermes agents running on Nemotron 3 Ultra through
> NemoClaw, each touching different NVIDIA / SaaS / Stripe surfaces.

## Tests

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

A pending approval will appear in the dashboard — click Approve or Reject.

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

Open the dashboard; the **Argus** tab should appear at the end. Clicking
it shows a "scaffold OK" card and a badge fed by
`GET /api/plugins/argus/health`.

## Layout

```
argus/
├── CLAUDE.md           # design doc — single source of truth
├── plugin.yaml         # Hermes plugin manifest (Python side)
├── __init__.py         # register(ctx) → wires pre_tool_call hook
├── hook.py             # Capture + Enforcement (synchronous hold)
├── policy.py           # pure decide() function
├── db.py               # ledger + approvals + audit (SQLite WAL)
├── config.py           # paths and cost-center loading
├── cost_centers.yaml.example
├── dashboard/
│   ├── manifest.json   # dashboard plugin manifest
│   ├── plugin_api.py   # FastAPI router → /api/plugins/argus/
│   └── dist/index.js   # BUILT IIFE — do not hand-edit
├── src/                # React source for the tab
│   ├── index.jsx
│   └── react-shim.js
├── build.mjs           # esbuild → dashboard/dist/index.js
├── package.json
├── requirements.txt
├── requirements-dev.txt
└── tests/
```
