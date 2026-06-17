#!/usr/bin/env python3
"""Argus demo driver — three unrelated jobs, one control plane.

Hits the running Argus plugin's HTTP API to simulate three Hermes agents
spending money in different ways. The point is to show Argus governing
heterogeneous jobs with a single ledger + policy.

Usage:
    export HERMES_DASHBOARD_SESSION_TOKEN=argus-demo
    # (start dashboard in another terminal first)
    python3 scripts/demo.py

The script blocks on every spend that needs human approval — go to the
Argus tab in the dashboard and click Approve/Reject. See DEMO.md for the
full walkthrough.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Dict


BASE = os.environ.get("ARGUS_BASE", "http://127.0.0.1:9119/api/plugins/argus")
TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "argus-demo")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Pretty printing — wide enough to read on a recorded screencast.
# ---------------------------------------------------------------------------


def banner(text: str) -> None:
    bar = "═" * 70
    print(f"\n{bar}\n  {text}\n{bar}")


def step(text: str) -> None:
    print(f"\n→ {text}")


def result(outcome: str, detail: str = "") -> None:
    glyph = {"allow": "✅", "block": "⛔", "revenue": "💰", "info": "  "}.get(
        outcome, "  "
    )
    print(f"   {glyph} {detail}")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no extra deps on the demo box).
# ---------------------------------------------------------------------------


def _request(method: str, path: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}


def spend(job_id: str, cost_center_id: str, amount: float, ref: str | None = None) -> Dict[str, Any]:
    return _request("POST", "/sim/spend", {
        "job_id": job_id,
        "cost_center_id": cost_center_id,
        "projected_usd": amount,
        "ref": ref,
    })


def revenue(job_id: str, amount: float, ref: str) -> Dict[str, Any]:
    return _request("POST", "/webhooks/stripe", {
        "type": "payment_intent.succeeded",
        "data": {"job_id": job_id, "amount_usd": amount, "id": ref},
    })


def pnl() -> Dict[str, Any]:
    return _request("GET", "/pnl")


# ---------------------------------------------------------------------------
# Each job is a function. The script runs them in order.
# ---------------------------------------------------------------------------


def job_a_pay_per_call_api() -> None:
    """50 sub-threshold API calls + 1 burst that needs manager approval."""
    banner("JOB A — Pay-per-call API agent (cost center: api_calls)")
    step("Agent makes 5 micro-calls at $0.02 each → all auto-approve")
    for i in range(5):
        r = spend("job-a-api", "api_calls", 0.02, ref=f"api_call_{i}")
        action = r.get("result", {}).get("action", "?")
        result("allow" if action == "allow" else "block", f"call #{i+1}  $0.02  → {action}")
        time.sleep(0.15)

    step("Now the agent decides to backfill a batch ($8) — over the auto threshold")
    print("   ⏸  Dashboard has a pending MANAGER approval. Click Approve or Reject.")
    r = spend("job-a-api", "api_calls", 8.0, ref="batch_backfill")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "batch_backfill  $8.00  → approved by human")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"batch_backfill  $8.00  → {msg}")

    step("Customer pays for the batch result → revenue webhook")
    revenue("job-a-api", 25.0, "pi_a_001")
    result("revenue", "Stripe webhook  +$25.00")


def job_b_saas_provisioning() -> None:
    """One large SaaS spend that routes to finance — the climactic beat."""
    banner("JOB B — SaaS provisioning agent (cost center: saas)")
    step("Agent wants to provision a Postgres tier on a SaaS partner — $79")
    print("   ⏸  Dashboard has a pending FINANCE approval. Click Approve.")
    r = spend("job-b-saas", "saas", 79.0, ref="postgres_tier_3yr")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "postgres_tier_3yr  $79.00  → approved by human")
        step("Provisioned. Service emits revenue on first use → $120")
        revenue("job-b-saas", 120.0, "pi_b_001")
        result("revenue", "Stripe webhook  +$120.00")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"postgres_tier_3yr  $79.00  → {msg}")
        result("info", "(no revenue — provisioning cancelled)")


def job_c_one_off_service() -> None:
    """Two service purchases: one rejected, one approved — shows both branches."""
    banner("JOB C — One-off service purchases (cost center: services)")
    step("Agent buys $7 of NVIDIA NIM inference credits")
    print("   ⏸  Dashboard has a pending MANAGER approval. **Reject** this one.")
    r = spend("job-c-services", "services", 7.0, ref="nim_credits_first")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "nim_credits_first  $7.00  → approved")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"nim_credits_first  $7.00  → {msg}")

    step("Agent retries with a smaller bundle: $3")
    print("   ⏸  Dashboard has a pending MANAGER approval. **Approve** this one.")
    r = spend("job-c-services", "services", 3.0, ref="nim_credits_retry")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "nim_credits_retry  $3.00  → approved by human")
        revenue("job-c-services", 9.0, "pi_c_001")
        result("revenue", "Stripe webhook  +$9.00")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"nim_credits_retry  $3.00  → {msg}")


def show_final_pnl() -> None:
    banner("FINAL P&L — three unrelated jobs, one Argus ledger")
    data = pnl()
    if "_http_error" in data:
        print(f"   ⚠  P&L fetch failed: {data}")
        return
    print(f"\n   {'Job':<22} {'Revenue':>10} {'LLM':>10} {'External':>10} {'P&L':>10}")
    print("   " + "─" * 64)
    for r in data.get("jobs", []):
        print(
            f"   {r['job_id']:<22} ${r['revenue']:>9.2f} ${r['llm_cost']:>9.2f} "
            f"${r['external_spend']:>9.2f} ${r['pnl']:>9.2f}"
        )
    t = data.get("total", {})
    print("   " + "─" * 64)
    print(
        f"   {'TOTAL':<22} ${t.get('revenue',0):>9.2f} ${t.get('llm_cost',0):>9.2f} "
        f"${t.get('external_spend',0):>9.2f} ${t.get('pnl',0):>9.2f}"
    )
    print()


def main() -> int:
    banner("Argus — three-job demo")
    print(f"  Hitting: {BASE}")
    print(f"  Token:   {TOKEN[:6]}…")
    # Smoke check
    h = _request("GET", "/health")
    if "_http_error" in h:
        print(f"\n  ✗ health check failed: {h}")
        print("    Is the dashboard running? Did you export HERMES_DASHBOARD_SESSION_TOKEN?")
        return 1
    print(f"  OK — telemetry_attached={h.get('telemetry_attached')}")

    job_a_pay_per_call_api()
    job_b_saas_provisioning()
    job_c_one_off_service()
    show_final_pnl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
