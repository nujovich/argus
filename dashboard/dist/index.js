(() => {
  // src/react-shim.js
  var React = window.__HERMES_PLUGIN_SDK__.React;

  // src/index.jsx
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var { React: React2 } = SDK;
  var { useEffect, useState, useCallback } = SDK.hooks;
  var { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  var POLL_MS = 1500;
  function fmtUsd(n) {
    const v = Number(n || 0);
    const sign = v < 0 ? "-" : "";
    return `${sign}$${Math.abs(v).toFixed(2)}`;
  }
  function fmtTime(ts) {
    if (!ts) return "";
    try {
      return new Date(ts).toLocaleString();
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
  function PnlSection() {
    const { data, error } = usePolling("/api/plugins/argus/pnl", POLL_MS);
    const rows = data?.jobs || [];
    const total = data?.total || { revenue: 0, llm_cost: 0, external_spend: 0, pnl: 0 };
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "P&L per job")), /* @__PURE__ */ React2.createElement(CardContent, null, error && /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive" }, "error: ", error), rows.length === 0 ? /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)" } }, "No ledger activity yet.") : /* @__PURE__ */ React2.createElement("table", { style: { width: "100%", borderCollapse: "collapse" } }, /* @__PURE__ */ React2.createElement("thead", null, /* @__PURE__ */ React2.createElement("tr", { style: { textAlign: "left", borderBottom: "1px solid var(--color-border)" } }, /* @__PURE__ */ React2.createElement("th", { style: { padding: "0.4rem 0.5rem" } }, "Job"), /* @__PURE__ */ React2.createElement("th", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, "Revenue"), /* @__PURE__ */ React2.createElement("th", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, "LLM cost"), /* @__PURE__ */ React2.createElement("th", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, "External"), /* @__PURE__ */ React2.createElement("th", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, "P&L"))), /* @__PURE__ */ React2.createElement("tbody", null, rows.map((r) => /* @__PURE__ */ React2.createElement("tr", { key: r.job_id, style: { borderBottom: "1px solid var(--color-border)" } }, /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem" } }, r.job_id), /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, fmtUsd(r.revenue)), /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, fmtUsd(r.llm_cost)), /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, fmtUsd(r.external_spend)), /* @__PURE__ */ React2.createElement("td", { style: {
      padding: "0.4rem 0.5rem",
      textAlign: "right",
      color: r.pnl >= 0 ? "var(--color-success)" : "var(--color-destructive)",
      fontWeight: 600
    } }, fmtUsd(r.pnl)))), /* @__PURE__ */ React2.createElement("tr", { style: { fontWeight: 700 } }, /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem" } }, "Total"), /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, fmtUsd(total.revenue)), /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, fmtUsd(total.llm_cost)), /* @__PURE__ */ React2.createElement("td", { style: { padding: "0.4rem 0.5rem", textAlign: "right" } }, fmtUsd(total.external_spend)), /* @__PURE__ */ React2.createElement("td", { style: {
      padding: "0.4rem 0.5rem",
      textAlign: "right",
      color: total.pnl >= 0 ? "var(--color-success)" : "var(--color-destructive)"
    } }, fmtUsd(total.pnl)))))));
  }
  function ApprovalsSection() {
    const { data, error, refresh } = usePolling("/api/plugins/argus/approvals?status=pending", POLL_MS);
    const items = data?.items || [];
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
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Approval queue (", items.length, " pending)")), /* @__PURE__ */ React2.createElement(CardContent, null, error && /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive" }, "error: ", error), items.length === 0 ? /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)" } }, "No pending approvals.") : /* @__PURE__ */ React2.createElement("div", { style: { display: "grid", gap: "0.75rem" } }, items.map((it) => /* @__PURE__ */ React2.createElement("div", { key: it.id, style: {
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius)",
      padding: "0.75rem 1rem",
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      gap: "1rem"
    } }, /* @__PURE__ */ React2.createElement("div", null, /* @__PURE__ */ React2.createElement("div", { style: { fontWeight: 600 } }, fmtUsd(it.projected_usd), " \u2014 ", it.cost_center_id, "  ", /* @__PURE__ */ React2.createElement(Badge, { variant: it.level === "finance" ? "destructive" : "secondary" }, it.level)), /* @__PURE__ */ React2.createElement("div", { style: { color: "var(--color-muted-foreground)", fontSize: "0.85em" } }, "job ", /* @__PURE__ */ React2.createElement("code", null, it.job_id), " via ", /* @__PURE__ */ React2.createElement("code", null, it.tool_name), " \xB7 ", fmtTime(it.created_at))), /* @__PURE__ */ React2.createElement("div", { style: { display: "flex", gap: "0.5rem" } }, /* @__PURE__ */ React2.createElement(Button, { onClick: () => decide(it.id, "approve") }, "Approve"), /* @__PURE__ */ React2.createElement(Button, { variant: "destructive", onClick: () => decide(it.id, "reject") }, "Reject")))))));
  }
  function AuditSection() {
    const { data, error } = usePolling("/api/plugins/argus/audit?limit=50", POLL_MS * 2);
    const items = data?.items || [];
    return /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Audit trail")), /* @__PURE__ */ React2.createElement(CardContent, null, error && /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive" }, "error: ", error), items.length === 0 ? /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)" } }, "No audit events yet.") : /* @__PURE__ */ React2.createElement("div", { style: { maxHeight: "320px", overflow: "auto", fontSize: "0.85em", fontFamily: "monospace" } }, items.map((r, i) => /* @__PURE__ */ React2.createElement("div", { key: i, style: { padding: "0.25rem 0", borderBottom: "1px solid var(--color-border)" } }, /* @__PURE__ */ React2.createElement("span", { style: { color: "var(--color-muted-foreground)" } }, fmtTime(r.ts)), "  ", /* @__PURE__ */ React2.createElement(Badge, { variant: "secondary" }, r.actor), "  ", /* @__PURE__ */ React2.createElement("strong", null, r.event), r.payload && /* @__PURE__ */ React2.createElement("span", { style: { color: "var(--color-muted-foreground)" } }, "  ", JSON.stringify(r.payload)))))));
  }
  function ArgusPage() {
    return /* @__PURE__ */ React2.createElement("div", { style: { padding: "1.5rem", display: "grid", gap: "1rem" } }, /* @__PURE__ */ React2.createElement(PnlSection, null), /* @__PURE__ */ React2.createElement(ApprovalsSection, null), /* @__PURE__ */ React2.createElement(AuditSection, null));
  }
  window.__HERMES_PLUGINS__.register("argus", ArgusPage);
})();
