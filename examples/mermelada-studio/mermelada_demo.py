#!/usr/bin/env python3
"""Mermelada Studio — deterministic demo of an agent earning + spending
through Argus's gating policy.

Walks an operator through one commission end-to-end:
  1. Customer pays $15 via Stripe (revenue webhook)
  2. Agent generates art (3 micro auto-approves + 1 manager escalation)
  3. Agent provisions premium fonts ($40 → reject → $5 retry → approve)
  4. Agent renders compute (auto-approve)
  5. Agent tries ads boost ($25 marketing → blocked, out-of-category)
  6. Final P&L summary

Drop ``cost_centers.yaml`` from this dir at ``~/.hermes/argus/`` before
running. See ``examples/mermelada-studio/README.md`` for the full
recipe.
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
# Pretty printing
# ---------------------------------------------------------------------------


def banner(text: str) -> None:
    bar = "═" * 72
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
# The commission
# ---------------------------------------------------------------------------


JOB = "mermelada-commission-001"


def stage_1_customer_pays() -> None:
    banner("STAGE 1 — Customer pays $15 for a 3-slide carousel")
    step("Stripe Checkout webhook fires → revenue lands in Argus's ledger")
    revenue(JOB, 15.0, "pi_mermelada_commission_001")
    result("revenue", "Stripe webhook  +$15.00")


def stage_2_image_generation() -> None:
    banner("STAGE 2 — Agent generates art (image_gen cost center)")
    step("3 background images @ $0.30 each — micro auto-approves")
    for i in range(3):
        r = spend(JOB, "image_gen", 0.30, ref=f"bg_image_{i+1}")
        action = r.get("result", {}).get("action", "?")
        result("allow" if action == "allow" else "block",
               f"bg_image_{i+1}  $0.30  → {action}")
        time.sleep(0.15)

    step("Hero illustration $2.00 — over the auto threshold of $0.50")
    print("   ⏸  Dashboard has a pending MANAGER approval. Click Approve.")
    r = spend(JOB, "image_gen", 2.0, ref="hero_illustration")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "hero_illustration  $2.00  → approved by human")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"hero_illustration  $2.00  → {msg}")


def stage_3_saas_provisioning() -> None:
    banner("STAGE 3 — Agent provisions premium fonts (saas_dev_tools)")
    step("Premium font license $40 — saas_dev_tools has no auto threshold")
    print("   ⏸  Dashboard has a pending MANAGER approval. **Reject** this one.")
    r = spend(JOB, "saas_dev_tools", 40.0, ref="premium_fonts_pack")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "premium_fonts_pack  $40.00  → approved")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"premium_fonts_pack  $40.00  → {msg}")
        result("info", "Agent self-corrects: try a cheaper alternative…")

    step("Agent retries with the standard font set: $5")
    print("   ⏸  Dashboard has a pending MANAGER approval. **Approve** this one.")
    r = spend(JOB, "saas_dev_tools", 5.0, ref="standard_fonts")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "standard_fonts  $5.00  → approved by human")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"standard_fonts  $5.00  → {msg}")


def stage_4_compute() -> None:
    banner("STAGE 4 — Render the carousel (compute cost center)")
    step("Own renderer pipeline $0.10 — well under auto threshold")
    r = spend(JOB, "compute", 0.10, ref="render_pipeline")
    action = r.get("result", {}).get("action", "?")
    result("allow" if action == "allow" else "block", f"render_pipeline  $0.10  → {action}")


def stage_5_greedy_ads() -> None:
    banner("STAGE 5 — Agent gets greedy: tries to boost reach with ads")
    step("Ads boost $25 (marketing) — limit_usd=0 → hard-cap breach, finance tier")
    print("   ⏸  Dashboard has a pending FINANCE approval. **Reject** this one (out-of-category).")
    r = spend(JOB, "marketing", 25.0, ref="ads_boost_attempt")
    action = r.get("result", {}).get("action")
    if action == "allow":
        result("allow", "ads_boost_attempt  $25.00  → approved (you let it through!)")
    else:
        msg = r.get("result", {}).get("message", "")
        result("block", f"ads_boost_attempt  $25.00  → {msg}")
        result("info", "Agent stops — marketing is policy-denied for this engagement.")


def stage_6_delivery() -> None:
    banner("STAGE 6 — Deliver to the customer")
    print("\n   The 3-slide carousel is rendered. The agent ships it to the customer.")
    print("   (In a live demo, this is where the mermelada-social-design skill runs.)")


def show_final_pnl() -> None:
    banner("FINAL P&L — One Mermelada commission, governed end-to-end by Argus")
    data = pnl()
    if "_http_error" in data:
        print(f"   ⚠  P&L fetch failed: {data}")
        return
    print(f"\n   {'Job':<32} {'Revenue':>10} {'LLM':>10} {'External':>10} {'P&L':>10}")
    print("   " + "─" * 74)
    for r in data.get("jobs", []):
        print(
            f"   {r['job_id']:<32} ${r['revenue']:>9.2f} ${r['llm_cost']:>9.2f} "
            f"${r['external_spend']:>9.2f} ${r['pnl']:>9.2f}"
        )
    t = data.get("total", {})
    print("   " + "─" * 74)
    print(
        f"   {'TOTAL':<32} ${t.get('revenue',0):>9.2f} ${t.get('llm_cost',0):>9.2f} "
        f"${t.get('external_spend',0):>9.2f} ${t.get('pnl',0):>9.2f}"
    )
    print()
    print("   Three policy verdicts exercised in one commission:")
    print("     • 4 auto-approves   (image-gen micro + compute)")
    print("     • 2 human escalations (font reject + retry approve)")
    print("     • 1 out-of-category block (ads boost)")
    print()
    print("   That is what production-grade agentic commerce looks like.")
    print()


def main() -> int:
    banner("Mermelada Studio — one commission, three policy verdicts")
    print(f"  Hitting: {BASE}")
    print(f"  Token:   {TOKEN[:6]}…")
    h = _request("GET", "/health")
    if "_http_error" in h:
        print(f"\n  ✗ health check failed: {h}")
        print("    Is the dashboard running? Did you export HERMES_DASHBOARD_SESSION_TOKEN?")
        return 1
    print(f"  OK — telemetry_attached={h.get('telemetry_attached')}")

    stage_1_customer_pays()
    stage_2_image_generation()
    stage_3_saas_provisioning()
    stage_4_compute()
    stage_5_greedy_ads()
    stage_6_delivery()
    show_final_pnl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
