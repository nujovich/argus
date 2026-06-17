(() => {
  // src/react-shim.js
  var React = window.__HERMES_PLUGIN_SDK__.React;

  // src/index.jsx
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var { React: React2 } = SDK;
  var { useEffect, useState, useCallback, useRef, useMemo } = SDK.hooks;
  var { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  var POLL_MS = 1500;
  var FAST_POLL_MS = 800;
  function fmtUsd(n) {
    const v = Number(n || 0);
    const sign = v < 0 ? "-" : "";
    return `${sign}$${Math.abs(v).toFixed(2)}`;
  }
  function fmtTime(ts) {
    if (!ts) return "";
    try {
      return new Date(ts).toLocaleTimeString();
    } catch {
      return ts;
    }
  }
  function usePolling(path, intervalMs) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const refresh = useCallback(() => {
      SDK.fetchJSON(path).then((d) => {
        setData(d);
        setError(null);
      }).catch((e) => setError(String(e)));
    }, [path]);
    useEffect(() => {
      refresh();
      const t = setInterval(refresh, intervalMs);
      return () => clearInterval(t);
    }, [refresh, intervalMs]);
    return { data, error, refresh };
  }
  function useAnimationsCSS() {
    useEffect(() => {
      const id = "argus-anim-css";
      if (document.getElementById(id)) return;
      const style = document.createElement("style");
      style.id = id;
      style.textContent = `
      @keyframes argus-pulse {
        0%   { box-shadow: 0 0 0 0 var(--color-warning, #f59e0b); }
        70%  { box-shadow: 0 0 0 12px transparent; }
        100% { box-shadow: 0 0 0 0 transparent; }
      }
      @keyframes argus-slide-in {
        from { opacity: 0; transform: translateY(-6px); }
        to   { opacity: 1; transform: translateY(0); }
      }
      .argus-pulse    { animation: argus-pulse 1.6s ease-out 2; }
      .argus-slide-in { animation: argus-slide-in 0.25s ease-out both; }
      .argus-num-tick { transition: color 0.5s ease, transform 0.3s ease; }
      .argus-stage-active { transform: scale(1.03); }
    `;
      document.head.appendChild(style);
    }, []);
  }
  var STAGES = [
    { key: "paid", icon: "\u{1F4B0}", label: "Customer pays", hint: "Stripe Checkout \u2192 revenue ledger row" },
    { key: "art", icon: "\u{1F3A8}", label: "Generate art", hint: "image_gen \u2014 micro auto-approves + hero" },
    { key: "provision", icon: "\u{1F48E}", label: "Provision SaaS", hint: "saas_dev_tools \u2014 escalates to human" },
    { key: "render", icon: "\u{1F5BC}\uFE0F", label: "Render carousel", hint: "compute \u2014 own renderer" },
    { key: "boost", icon: "\u{1F6AB}", label: "Boost attempt", hint: "marketing \u2014 denied category" },
    { key: "deliver", icon: "\u{1F4E6}", label: "Deliver", hint: "Commission complete" }
  ];
  function deriveStageStatus(auditItems) {
    const status = Object.fromEntries(STAGES.map((s) => [s.key, "pending"]));
    if (!auditItems) return status;
    for (const r of auditItems) {
      const p = r.payload || {};
      const ev = r.event;
      const cc = p.cost_center_id;
      if (ev === "revenue_received") status.paid = "done";
      if ((ev === "auth_token_issued" || ev === "spend_evaluated") && cc === "image_gen")
        status.art = status.art === "done" ? "done" : "active";
      if (ev === "spend_resumed" && p.approval_id) status.art = "done";
      if (ev === "approval_requested" && cc === "image_gen") status.art = "human";
      if ((ev === "spend_evaluated" || ev === "approval_requested") && cc === "saas_dev_tools")
        status.provision = "human";
      if (ev === "spend_resumed" && status.provision === "human") status.provision = "done";
      if (ev === "spend_rejected" && status.provision === "human") status.provision = "rejected";
      if ((ev === "auth_token_issued" || ev === "spend_evaluated") && cc === "compute")
        status.render = "done";
      if ((ev === "spend_evaluated" || ev === "approval_requested") && cc === "marketing")
        status.boost = "human";
      if (ev === "spend_rejected" && status.boost === "human") status.boost = "blocked";
      if (status.render === "done" && (status.boost === "blocked" || status.boost === "done"))
        status.deliver = "done";
    }
    const order = STAGES.map((s) => s.key);
    for (const k of order) {
      if (status[k] === "pending") {
        status[k] = "next";
        break;
      }
    }
    return status;
  }
  function StageBadge({ status }) {
    const map = {
      pending: { text: "pending", variant: "secondary" },
      next: { text: "next", variant: "secondary" },
      active: { text: "in progress", variant: "secondary" },
      human: { text: "human required", variant: "destructive" },
      done: { text: "done", variant: "default" },
      rejected: { text: "rejected", variant: "destructive" },
      blocked: { text: "blocked", variant: "destructive" }
    };
    const m = map[status] || map.pending;
    return /* @__PURE__ */ React2.createElement(Badge, { variant: m.variant }, m.text);
  }
  function WorkflowTimeline({ statuses }) {
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Mermelada Studio \u2014 commission workflow")), /* @__PURE__ */ React2.createElement(CardContent, null, /* @__PURE__ */ React2.createElement("div", { style: { display: "grid", gridTemplateColumns: `repeat(${STAGES.length}, 1fr)`, gap: "0.5rem" } }, STAGES.map((s) => {
      const st = statuses[s.key];
      const isActive = st === "human" || st === "active";
      const isDone = st === "done";
      const isBlocked = st === "rejected" || st === "blocked";
      const tint = isDone ? "var(--color-success, #16a34a)" : isBlocked ? "var(--color-destructive)" : isActive ? "var(--color-warning, #f59e0b)" : "var(--color-border)";
      return /* @__PURE__ */ React2.createElement(
        "div",
        {
          key: s.key,
          className: isActive ? "argus-stage-active argus-pulse" : "",
          style: {
            border: `2px solid ${tint}`,
            borderRadius: "var(--radius)",
            padding: "0.75rem",
            textAlign: "center",
            transition: "transform 0.3s ease, border-color 0.5s ease",
            background: isDone ? "rgba(22,163,74,0.08)" : isBlocked ? "rgba(220,38,38,0.08)" : "transparent"
          }
        },
        /* @__PURE__ */ React2.createElement("div", { style: { fontSize: "1.6em" } }, s.icon),
        /* @__PURE__ */ React2.createElement("div", { style: { fontWeight: 600, marginTop: "0.25rem", fontSize: "0.9em" } }, s.label),
        /* @__PURE__ */ React2.createElement("div", { style: { marginTop: "0.4rem" } }, /* @__PURE__ */ React2.createElement(StageBadge, { status: st })),
        /* @__PURE__ */ React2.createElement("div", { style: { fontSize: "0.72em", color: "var(--color-muted-foreground)", marginTop: "0.3rem" } }, s.hint)
      );
    }))));
  }
  function AnimatedUsd({ value, big, positiveColor }) {
    const prev = useRef(value);
    const [delta, setDelta] = useState(null);
    useEffect(() => {
      if (prev.current !== value) {
        setDelta(value - prev.current);
        prev.current = value;
        const t = setTimeout(() => setDelta(null), 1200);
        return () => clearTimeout(t);
      }
    }, [value]);
    const color = positiveColor ? value >= 0 ? "var(--color-success, #16a34a)" : "var(--color-destructive)" : "var(--color-foreground)";
    return /* @__PURE__ */ React2.createElement(
      "div",
      {
        className: "argus-num-tick",
        style: {
          fontSize: big ? "1.8em" : "1.1em",
          fontWeight: 700,
          color,
          transform: delta ? "scale(1.04)" : "scale(1.0)"
        }
      },
      fmtUsd(value),
      delta !== null && Math.abs(delta) > 1e-3 && /* @__PURE__ */ React2.createElement("span", { style: {
        marginLeft: "0.5rem",
        fontSize: "0.55em",
        fontWeight: 500,
        color: delta > 0 ? "var(--color-success, #16a34a)" : "var(--color-destructive)"
      } }, delta > 0 ? "+" : "", fmtUsd(delta).replace("$", "$"))
    );
  }
  function PnLSummary({ pnlData }) {
    const t = pnlData?.total || { revenue: 0, llm_cost: 0, external_spend: 0, pnl: 0 };
    const tiles = [
      { label: "Revenue", value: t.revenue, big: true },
      { label: "LLM cost (Nemotron)", value: t.llm_cost },
      { label: "External spend", value: t.external_spend },
      { label: "P&L", value: t.pnl, big: true, positiveColor: true }
    ];
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Live P&L")), /* @__PURE__ */ React2.createElement(CardContent, null, /* @__PURE__ */ React2.createElement("div", { style: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem" } }, tiles.map((tile) => /* @__PURE__ */ React2.createElement(
      "div",
      {
        key: tile.label,
        style: {
          padding: "1rem",
          borderRadius: "var(--radius)",
          border: "1px solid var(--color-border)"
        }
      },
      /* @__PURE__ */ React2.createElement("div", { style: { fontSize: "0.8em", color: "var(--color-muted-foreground)" } }, tile.label),
      /* @__PURE__ */ React2.createElement(AnimatedUsd, { value: tile.value, big: tile.big, positiveColor: tile.positiveColor })
    ))), pnlData?.jobs?.length > 0 && /* @__PURE__ */ React2.createElement("div", { style: { marginTop: "1rem", fontSize: "0.85em", color: "var(--color-muted-foreground)" } }, pnlData.jobs.map((j) => /* @__PURE__ */ React2.createElement("span", { key: j.job_id, style: { marginRight: "1rem" } }, /* @__PURE__ */ React2.createElement("code", null, j.job_id), ": ", fmtUsd(j.pnl))))));
  }
  function TokenVault({ tokens }) {
    const items = tokens?.items || [];
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Active auth tokens (", items.length, ")")), /* @__PURE__ */ React2.createElement(CardContent, null, items.length === 0 ? /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)", fontSize: "0.9em" } }, "No active tokens. Each ALLOW issues a 60-second single-use token. Stripe spends without one are blocked.") : /* @__PURE__ */ React2.createElement("div", { style: { display: "grid", gap: "0.4rem" } }, items.map((t) => /* @__PURE__ */ React2.createElement(
      "div",
      {
        key: t.token_preview,
        className: "argus-slide-in",
        style: {
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "0.4rem 0.6rem",
          border: "1px solid var(--color-border)",
          borderRadius: "var(--radius)",
          fontSize: "0.85em",
          fontFamily: "monospace"
        }
      },
      /* @__PURE__ */ React2.createElement("span", null, /* @__PURE__ */ React2.createElement(Badge, null, "\u{1F511} ", t.token_preview), "  ", t.cost_center_id, " \u2014 ", fmtUsd(t.amount_usd)),
      /* @__PURE__ */ React2.createElement("span", { style: { color: "var(--color-muted-foreground)" } }, "job ", /* @__PURE__ */ React2.createElement("code", null, t.job_id))
    )))));
  }
  function ApprovalsSection() {
    const { data, error, refresh } = usePolling("/api/plugins/argus/approvals?status=pending", FAST_POLL_MS);
    const items = data?.items || [];
    const seenIdsRef = useRef(/* @__PURE__ */ new Set());
    const [pulsing, setPulsing] = useState(/* @__PURE__ */ new Set());
    useEffect(() => {
      const fresh = /* @__PURE__ */ new Set();
      items.forEach((it) => {
        if (!seenIdsRef.current.has(it.id)) fresh.add(it.id);
        seenIdsRef.current.add(it.id);
      });
      if (fresh.size) {
        setPulsing(fresh);
        const t = setTimeout(() => setPulsing(/* @__PURE__ */ new Set()), 2e3);
        return () => clearTimeout(t);
      }
    }, [items]);
    const decide = async (id, decision) => {
      try {
        await SDK.fetchJSON(`/api/plugins/argus/approvals/${id}/decide`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision, actor: "human:dashboard" })
        });
        refresh();
      } catch (e) {
        alert(`decide failed: ${e}`);
      }
    };
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Approval queue (", items.length, " pending)")), /* @__PURE__ */ React2.createElement(CardContent, null, error && /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive" }, "error: ", error), items.length === 0 ? /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)" } }, "No pending approvals.") : /* @__PURE__ */ React2.createElement("div", { style: { display: "grid", gap: "0.75rem" } }, items.map((it) => /* @__PURE__ */ React2.createElement(
      "div",
      {
        key: it.id,
        className: pulsing.has(it.id) ? "argus-pulse argus-slide-in" : "argus-slide-in",
        style: {
          border: "1px solid var(--color-border)",
          borderRadius: "var(--radius)",
          padding: "0.75rem 1rem",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "1rem",
          background: "var(--color-card)"
        }
      },
      /* @__PURE__ */ React2.createElement("div", null, /* @__PURE__ */ React2.createElement("div", { style: { fontWeight: 600, fontSize: "1.05em" } }, fmtUsd(it.projected_usd), " \u2014 ", it.cost_center_id, "  ", /* @__PURE__ */ React2.createElement(Badge, { variant: it.level === "finance" ? "destructive" : "secondary" }, it.level)), /* @__PURE__ */ React2.createElement("div", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85em" } }, "job ", /* @__PURE__ */ React2.createElement("code", null, it.job_id), " \xB7 ref ", /* @__PURE__ */ React2.createElement("code", null, it.ref || "\u2014"), " \xB7 ", fmtTime(it.created_at))),
      /* @__PURE__ */ React2.createElement("div", { style: { display: "flex", gap: "0.5rem" } }, /* @__PURE__ */ React2.createElement(Button, { onClick: () => decide(it.id, "approve") }, "Approve"), /* @__PURE__ */ React2.createElement(Button, { variant: "destructive", onClick: () => decide(it.id, "reject") }, "Reject"))
    )))));
  }
  var EVENT_TINT = {
    revenue_received: { color: "var(--color-success, #16a34a)", label: "REVENUE" },
    spend_evaluated: { color: "var(--color-muted-foreground)", label: "evaluated" },
    auth_token_issued: { color: "var(--color-success, #16a34a)", label: "token" },
    approval_requested: { color: "var(--color-warning, #f59e0b)", label: "needs approval" },
    approval_approved: { color: "var(--color-success, #16a34a)", label: "APPROVED" },
    approval_rejected: { color: "var(--color-destructive)", label: "REJECTED" },
    spend_resumed: { color: "var(--color-success, #16a34a)", label: "resumed" },
    spend_rejected: { color: "var(--color-destructive)", label: "blocked" },
    spend_timeout: { color: "var(--color-destructive)", label: "timeout" },
    stripe_blocked_no_token: { color: "var(--color-destructive)", label: "\u{1F6A8} rogue blocked" },
    stripe_blocked_bad_token: { color: "var(--color-destructive)", label: "\u{1F6A8} bad token" },
    stripe_blocked_no_amount: { color: "var(--color-destructive)", label: "\u{1F6A8} no amount" },
    stripe_authorized: { color: "var(--color-success, #16a34a)", label: "stripe ok" },
    refund_recorded: { color: "var(--color-warning, #f59e0b)", label: "refund" },
    webhook_ignored: { color: "var(--color-muted-foreground)", label: "ignored" },
    spend_skipped_missing_declaration: { color: "var(--color-muted-foreground)", label: "skipped" }
  };
  function AuditSection() {
    const { data, error } = usePolling("/api/plugins/argus/audit?limit=40", FAST_POLL_MS);
    const items = data?.items || [];
    const lastFirstRef = useRef(null);
    const [newKey, setNewKey] = useState(null);
    useEffect(() => {
      if (items[0]) {
        const sig = items[0].ts + items[0].event;
        if (lastFirstRef.current && lastFirstRef.current !== sig) setNewKey(sig);
        lastFirstRef.current = sig;
      }
    }, [items]);
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Live event stream")), /* @__PURE__ */ React2.createElement(CardContent, null, error && /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive" }, "error: ", error), items.length === 0 ? /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)" } }, 'No events yet \u2014 hit "Start commission" to begin.') : /* @__PURE__ */ React2.createElement("div", { style: { maxHeight: "320px", overflow: "auto", fontSize: "0.85em" } }, items.map((r, i) => {
      const tint = EVENT_TINT[r.event] || { color: "var(--color-muted-foreground)", label: r.event };
      const p = r.payload || {};
      const sig = r.ts + r.event;
      return /* @__PURE__ */ React2.createElement(
        "div",
        {
          key: i,
          className: sig === newKey ? "argus-slide-in" : "",
          style: {
            display: "grid",
            gridTemplateColumns: "100px 100px 1fr",
            gap: "0.5rem",
            padding: "0.3rem 0",
            borderBottom: "1px solid var(--color-border)",
            alignItems: "center"
          }
        },
        /* @__PURE__ */ React2.createElement("span", { style: { color: "var(--color-muted-foreground)", fontFamily: "monospace" } }, fmtTime(r.ts)),
        /* @__PURE__ */ React2.createElement("span", null, /* @__PURE__ */ React2.createElement(Badge, { style: { background: tint.color, color: "white" } }, tint.label)),
        /* @__PURE__ */ React2.createElement("span", { style: { fontFamily: "monospace", color: "var(--color-muted-foreground)" } }, p.cost_center_id && /* @__PURE__ */ React2.createElement("span", null, p.cost_center_id, " "), p.projected_usd != null && /* @__PURE__ */ React2.createElement("span", null, fmtUsd(p.projected_usd), " "), p.amount_usd != null && /* @__PURE__ */ React2.createElement("span", null, fmtUsd(p.amount_usd), " "), p.reason && /* @__PURE__ */ React2.createElement("span", null, "\xB7 ", p.reason), p.verdict && /* @__PURE__ */ React2.createElement("span", null, "\xB7 ", p.verdict), p.ref && /* @__PURE__ */ React2.createElement("span", null, " \xB7 ", /* @__PURE__ */ React2.createElement("code", null, p.ref)))
      );
    }))));
  }
  function StartCommissionButton() {
    const [running, setRunning] = useState(false);
    const [error, setError] = useState(null);
    const start = async () => {
      setRunning(true);
      setError(null);
      try {
        await SDK.fetchJSON("/api/plugins/argus/demo/mermelada/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}"
        });
      } catch (e) {
        setError(String(e));
      } finally {
        setRunning(false);
      }
    };
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Start a Mermelada commission")), /* @__PURE__ */ React2.createElement(CardContent, null, /* @__PURE__ */ React2.createElement("div", { style: { display: "flex", gap: "1rem", alignItems: "center" } }, /* @__PURE__ */ React2.createElement(Button, { onClick: start, disabled: running }, running ? "\u23F3 Commission in progress \u2014 approve in queue above" : "\u25B6 Start commission"), /* @__PURE__ */ React2.createElement("span", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85em" } }, "Triggers the full earn-and-spend loop. Approvals appear in the queue below.")), error && /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive", style: { marginTop: "0.5rem" } }, "error: ", error)));
  }
  function ArgusPage() {
    useAnimationsCSS();
    const { data: pnlData } = usePolling("/api/plugins/argus/pnl", POLL_MS);
    const { data: tokenData } = usePolling("/api/plugins/argus/tokens/active", FAST_POLL_MS);
    const { data: auditDataForStages } = usePolling("/api/plugins/argus/audit?limit=200", POLL_MS);
    const stageStatuses = useMemo(() => {
      const items = (auditDataForStages?.items || []).slice().reverse();
      return deriveStageStatus(items);
    }, [auditDataForStages]);
    return /* @__PURE__ */ React2.createElement("div", { style: { padding: "1.5rem", display: "grid", gap: "1rem" } }, /* @__PURE__ */ React2.createElement(WorkflowTimeline, { statuses: stageStatuses }), /* @__PURE__ */ React2.createElement(StartCommissionButton, null), /* @__PURE__ */ React2.createElement(PnLSummary, { pnlData }), /* @__PURE__ */ React2.createElement(ApprovalsSection, null), /* @__PURE__ */ React2.createElement(TokenVault, { tokens: tokenData }), /* @__PURE__ */ React2.createElement(AuditSection, null));
  }
  window.__HERMES_PLUGINS__.register("argus", ArgusPage);
})();
