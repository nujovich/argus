#!/usr/bin/env python3
"""AI Services Firm — Argus compute-allocation demo (Phase 4.5).

Walks an operator through three jobs from an autonomous AI services
firm, each with a different margin profile. Argus assigns a Nemotron
tier per job:

  1. Premium enterprise research ($200 commission, ~$15 burn) → ULTRA
  2. Low-margin generation ($3 commission, $5 burn requested)    → REJECT
     Agent self-corrects with a smaller burn (-1.50 margin still bad)
     → tries $3 revenue $0.30 burn (≤ ultra_min_revenue) → BASE
  3. Mid-flight throttle scenario (declared $5 burn, actual $4 racing) →
     downgrade order (Phase 4.5c). For now the deterministic script
     simulates the steady-state outcome.

Run with the AI services cost_centers.yaml dropped at
``~/.hermes/argus/cost_centers.yaml``.
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


def banner(text: str) -> None:
    bar = "═" * 72
    print(f"\n{bar}\n  {text}\n{bar}")


def step(text: str) -> None:
    print(f"\n→ {text}")


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers=HEADERS, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}


def _get(path: str) -> Dict[str, Any]:
    req = urllib.request.Request(BASE + path, headers=HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")}


def compute(job_id, cost_center_id, expected_revenue, projected_burn, ref):
    return _post("/sim/compute", {
        "job_id": job_id,
        "cost_center_id": cost_center_id,
        "expected_revenue_usd": expected_revenue,
        "projected_burn_usd": projected_burn,
        "ref": ref,
    })


def revenue(job_id, amount, ref):
    return _post("/webhooks/stripe", {
        "type": "payment_intent.succeeded",
        "data": {"job_id": job_id, "amount_usd": amount, "id": ref},
    })


def llm_cost(job_id, amount, ref):
    """Simulate Nemotron consumption against a job. Writes an llm_cost
    ledger row directly (in a live run this would come from the
    hermes-telemetry ATTACH join automatically)."""
    return _post("/admin/llm_cost", {
        "job_id": job_id, "amount_usd": amount, "ref": ref,
    })


def show(result, label):
    r = result.get("result", {})
    if r.get("action") == "allow":
        print(f"   ✅ {label}  → {r.get('tier','').upper()}  model={r.get('model','')}")
        print(f"      budget=${r.get('compute_budget_usd',0):.2f}  "
              f"expected_margin=${r.get('expected_margin_usd',0):.2f}  "
              f"🔑 token={(r.get('auth_token') or '')[:8]}…")
    else:
        print(f"   ⛔ {label}  → {r.get('verdict','BLOCK')}  reason={r.get('reason') or r.get('message','')}")


def job_a_premium_research():
    banner("JOB A — Enterprise research ($200 commission, projected $15 burn)")
    step("Agent declares compute: expected_revenue=$200, projected_burn=$15")
    r = compute("research-enterprise-001", "ai_research", 200.0, 15.0,
                "competitor_landscape_q3")
    show(r, "research-enterprise-001")

    step("Customer pays the $200 commission (Stripe webhook)")
    revenue("research-enterprise-001", 200.0, "pi_research_001")
    print("   💰 +$200.00 revenue")

    step("Agent runs deep research on Nemotron 3 Ultra (simulated ~$15 burn)")
    llm_cost("research-enterprise-001", 14.80, "nemotron_ultra_session_1")
    print("   ⚙  Nemotron Ultra burned $14.80")


def job_b_low_margin_generation():
    banner("JOB B — Generation ($3 commission, $5 requested burn → too thin)")
    step("Agent declares compute: expected_revenue=$3, projected_burn=$5")
    r = compute("gen-tweet-042", "ai_generation", 3.0, 5.0, "viral_tweet")
    show(r, "gen-tweet-042 (over-spec)")
    print("   ↪ Agent must self-correct…")

    step("Agent retries with much smaller burn: $0.30 — fits the margin")
    r = compute("gen-tweet-042", "ai_generation", 3.0, 0.30, "viral_tweet_v2")
    show(r, "gen-tweet-042 (v2)")

    step("Customer pays the $3 commission")
    revenue("gen-tweet-042", 3.0, "pi_gen_042")
    print("   💰 +$3.00 revenue")

    step("Agent runs generation on the assigned tier ($0.30 simulated)")
    llm_cost("gen-tweet-042", 0.28, "nemotron_session_b")
    print("   ⚙  $0.28 of inference burned")


def job_c_negative_margin_rejected():
    banner("JOB C — Vanity research ($2 commission, $20 burn → reject)")
    step("Agent declares compute: expected_revenue=$2, projected_burn=$20")
    r = compute("research-vanity-099", "ai_research", 2.0, 20.0, "vanity_lookup")
    show(r, "research-vanity-099")
    print("   ↪ Argus refuses to spin up Ultra (or anything) for negative-margin work.")


def show_fleet():
    banner("FLEET VIEW — Argus allocates compute as capital, across the fleet")
    data = _get("/compute/fleet")
    items = data.get("items", [])
    if not items:
        print("   (no allocations yet)")
        return
    print(f"\n   {'Job':<32} {'Tier':<8} {'Budget':>10} {'Burn':>10} {'Revenue':>10} {'Margin':>10}")
    print("   " + "─" * 90)
    for it in items:
        print(
            f"   {it['job_id']:<32} {it['tier']:<8} "
            f"${it['compute_budget_usd']:>9.2f} ${it['actual_burn_usd']:>9.2f} "
            f"${it['actual_revenue_usd']:>9.2f} ${it['current_margin_usd']:>9.2f}"
        )
    print()


def show_pnl():
    banner("FINAL P&L — Three jobs, one allocator, capital flowing toward margin")
    data = _get("/pnl")
    if "_http_error" in data:
        return
    print(f"\n   {'Job':<32} {'Revenue':>10} {'LLM':>10} {'External':>10} {'P&L':>10}")
    print("   " + "─" * 76)
    for r in data.get("jobs", []):
        print(
            f"   {r['job_id']:<32} ${r['revenue']:>9.2f} ${r['llm_cost']:>9.2f} "
            f"${r['external_spend']:>9.2f} ${r['pnl']:>9.2f}"
        )
    t = data.get("total", {})
    print("   " + "─" * 76)
    print(
        f"   {'TOTAL':<32} ${t.get('revenue',0):>9.2f} ${t.get('llm_cost',0):>9.2f} "
        f"${t.get('external_spend',0):>9.2f} ${t.get('pnl',0):>9.2f}"
    )


def main() -> int:
    banner("Argus — AI Services Firm demo (Phase 4.5)")
    print(f"  Hitting: {BASE}")
    h = _get("/health")
    if "_http_error" in h:
        print(f"\n  ✗ health check failed: {h}")
        return 1

    job_a_premium_research()
    job_b_low_margin_generation()
    job_c_negative_margin_rejected()
    show_fleet()
    show_pnl()
    print("\n  Closing line for the screencast:")
    print("    'Stripe gives the wallet. NemoClaw isolates the process.")
    print("     NVIDIA sells the compute. Argus allocates all three as")
    print("     capital, in real time, toward margin. Deploy it on DGX Spark.'\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
