# Argus

> Stripe gives agents a wallet; Argus puts a hundred eyes on it.

Horizontal financial control plane for money-spending Hermes agents.
Meters every dollar in/out per job, tracks live P&L, and gates Stripe
spends through a human-in-the-loop approval flow.

**Status:** Phase 2 scaffold — empty plugin that renders an Argus tab in
the Hermes dashboard and proves front-end ↔ back-end wiring. No ledger,
no Stripe, no policy yet. Full design in [`CLAUDE.md`](./CLAUDE.md).

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
├── __init__.py         # register(ctx) — no-op in scaffold
├── dashboard/
│   ├── manifest.json   # dashboard plugin manifest
│   ├── plugin_api.py   # FastAPI router → /api/plugins/argus/
│   └── dist/index.js   # BUILT IIFE — do not hand-edit
├── src/                # React source for the tab
│   ├── index.jsx
│   └── react-shim.js
├── build.mjs           # esbuild → dashboard/dist/index.js
└── package.json
```
