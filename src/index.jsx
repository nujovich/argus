/* Argus dashboard — P&L, approval queue, audit trail.
 *
 * Reads window.__HERMES_PLUGIN_SDK__ — never bundles React. See CLAUDE.md §7.
 * Polls /api/plugins/argus/{pnl,approvals,audit} every 1.5s.
 */

const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useEffect, useState, useCallback } = SDK.hooks;
const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;

const POLL_MS = 1500;

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
    SDK.fetchJSON(path)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
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
  return (
    <Card>
      <CardHeader>
        <CardTitle>P&amp;L per job</CardTitle>
      </CardHeader>
      <CardContent>
        {error && <Badge variant="destructive">error: {error}</Badge>}
        {rows.length === 0 ? (
          <p style={{ color: "var(--color-muted-foreground)" }}>No ledger activity yet.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid var(--color-border)" }}>
                <th style={{ padding: "0.4rem 0.5rem" }}>Job</th>
                <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>Revenue</th>
                <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>LLM cost</th>
                <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>External</th>
                <th style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.job_id} style={{ borderBottom: "1px solid var(--color-border)" }}>
                  <td style={{ padding: "0.4rem 0.5rem" }}>{r.job_id}</td>
                  <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{fmtUsd(r.revenue)}</td>
                  <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{fmtUsd(r.llm_cost)}</td>
                  <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{fmtUsd(r.external_spend)}</td>
                  <td style={{
                    padding: "0.4rem 0.5rem",
                    textAlign: "right",
                    color: r.pnl >= 0 ? "var(--color-success)" : "var(--color-destructive)",
                    fontWeight: 600,
                  }}>{fmtUsd(r.pnl)}</td>
                </tr>
              ))}
              <tr style={{ fontWeight: 700 }}>
                <td style={{ padding: "0.4rem 0.5rem" }}>Total</td>
                <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{fmtUsd(total.revenue)}</td>
                <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{fmtUsd(total.llm_cost)}</td>
                <td style={{ padding: "0.4rem 0.5rem", textAlign: "right" }}>{fmtUsd(total.external_spend)}</td>
                <td style={{
                  padding: "0.4rem 0.5rem",
                  textAlign: "right",
                  color: total.pnl >= 0 ? "var(--color-success)" : "var(--color-destructive)",
                }}>{fmtUsd(total.pnl)}</td>
              </tr>
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

function ApprovalsSection() {
  const { data, error, refresh } = usePolling("/api/plugins/argus/approvals?status=pending", POLL_MS);
  const items = data?.items || [];
  const decide = async (id, decision) => {
    try {
      await SDK.fetchJSON(`/api/plugins/argus/approvals/${id}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, actor: "human:dashboard" }),
      });
      refresh();
    } catch (e) {
      alert(`decide failed: ${e}`);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Approval queue ({items.length} pending)</CardTitle>
      </CardHeader>
      <CardContent>
        {error && <Badge variant="destructive">error: {error}</Badge>}
        {items.length === 0 ? (
          <p style={{ color: "var(--color-muted-foreground)" }}>No pending approvals.</p>
        ) : (
          <div style={{ display: "grid", gap: "0.75rem" }}>
            {items.map((it) => (
              <div key={it.id} style={{
                border: "1px solid var(--color-border)",
                borderRadius: "var(--radius)",
                padding: "0.75rem 1rem",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "1rem",
              }}>
                <div>
                  <div style={{ fontWeight: 600 }}>
                    {fmtUsd(it.projected_usd)} — {it.cost_center_id}
                    {"  "}
                    <Badge variant={it.level === "finance" ? "destructive" : "secondary"}>{it.level}</Badge>
                  </div>
                  <div style={{ color: "var(--color-muted-foreground)", fontSize: "0.85em" }}>
                    job <code>{it.job_id}</code> via <code>{it.tool_name}</code> · {fmtTime(it.created_at)}
                  </div>
                </div>
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  <Button onClick={() => decide(it.id, "approve")}>Approve</Button>
                  <Button variant="destructive" onClick={() => decide(it.id, "reject")}>Reject</Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AuditSection() {
  const { data, error } = usePolling("/api/plugins/argus/audit?limit=50", POLL_MS * 2);
  const items = data?.items || [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit trail</CardTitle>
      </CardHeader>
      <CardContent>
        {error && <Badge variant="destructive">error: {error}</Badge>}
        {items.length === 0 ? (
          <p style={{ color: "var(--color-muted-foreground)" }}>No audit events yet.</p>
        ) : (
          <div style={{ maxHeight: "320px", overflow: "auto", fontSize: "0.85em", fontFamily: "monospace" }}>
            {items.map((r, i) => (
              <div key={i} style={{ padding: "0.25rem 0", borderBottom: "1px solid var(--color-border)" }}>
                <span style={{ color: "var(--color-muted-foreground)" }}>{fmtTime(r.ts)}</span>
                {"  "}
                <Badge variant="secondary">{r.actor}</Badge>
                {"  "}
                <strong>{r.event}</strong>
                {r.payload && (
                  <span style={{ color: "var(--color-muted-foreground)" }}>
                    {"  "}{JSON.stringify(r.payload)}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ArgusPage() {
  return (
    <div style={{ padding: "1.5rem", display: "grid", gap: "1rem" }}>
      <PnlSection />
      <ApprovalsSection />
      <AuditSection />
    </div>
  );
}

window.__HERMES_PLUGINS__.register("argus", ArgusPage);
