/* Argus dashboard — workflow-first view.
 *
 * Single surface that tells the whole commission story: workflow
 * timeline + live event stream + approval queue (animated) + token
 * vault widget + P&L. No terminal required during the demo — the
 * "Start commission" button drives the flow from the browser.
 *
 * Reads window.__HERMES_PLUGIN_SDK__ — never bundles React.
 * See CLAUDE.md §7 for the view-layer rules.
 */

const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useEffect, useState, useCallback, useRef, useMemo } = SDK.hooks;
const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;

const POLL_MS = 1500;
const FAST_POLL_MS = 800;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

// Inject keyframes once.
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

// ---------------------------------------------------------------------------
// Workflow Timeline — derives a 6-stage stepper from recent audit events
// ---------------------------------------------------------------------------

const STAGES = [
  { key: "paid",      icon: "💰", label: "Customer pays",     hint: "Stripe Checkout → revenue ledger row" },
  { key: "art",       icon: "🎨", label: "Generate art",      hint: "image_gen — micro auto-approves + hero" },
  { key: "provision", icon: "💎", label: "Provision SaaS",    hint: "saas_dev_tools — escalates to human" },
  { key: "render",    icon: "🖼️", label: "Render carousel",   hint: "compute — own renderer" },
  { key: "boost",     icon: "🚫", label: "Boost attempt",     hint: "marketing — denied category" },
  { key: "deliver",   icon: "📦", label: "Deliver",           hint: "Commission complete" },
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
  // The "first pending" becomes the active stage if nothing else is.
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
    pending:  { text: "pending",  variant: "secondary" },
    next:     { text: "next",     variant: "secondary" },
    active:   { text: "in progress", variant: "secondary" },
    human:    { text: "human required", variant: "destructive" },
    done:     { text: "done",     variant: "default" },
    rejected: { text: "rejected", variant: "destructive" },
    blocked:  { text: "blocked",  variant: "destructive" },
  };
  const m = map[status] || map.pending;
  return <Badge variant={m.variant}>{m.text}</Badge>;
}

function WorkflowTimeline({ statuses }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Mermelada Studio — commission workflow</CardTitle>
      </CardHeader>
      <CardContent>
        <div style={{ display: "grid", gridTemplateColumns: `repeat(${STAGES.length}, 1fr)`, gap: "0.5rem" }}>
          {STAGES.map((s) => {
            const st = statuses[s.key];
            const isActive = st === "human" || st === "active";
            const isDone = st === "done";
            const isBlocked = st === "rejected" || st === "blocked";
            const tint = isDone
              ? "var(--color-success, #16a34a)"
              : isBlocked
              ? "var(--color-destructive)"
              : isActive
              ? "var(--color-warning, #f59e0b)"
              : "var(--color-border)";
            return (
              <div
                key={s.key}
                className={isActive ? "argus-stage-active argus-pulse" : ""}
                style={{
                  border: `2px solid ${tint}`,
                  borderRadius: "var(--radius)",
                  padding: "0.75rem",
                  textAlign: "center",
                  transition: "transform 0.3s ease, border-color 0.5s ease",
                  background: isDone ? "rgba(22,163,74,0.08)" : isBlocked ? "rgba(220,38,38,0.08)" : "transparent",
                }}
              >
                <div style={{ fontSize: "1.6em" }}>{s.icon}</div>
                <div style={{ fontWeight: 600, marginTop: "0.25rem", fontSize: "0.9em" }}>{s.label}</div>
                <div style={{ marginTop: "0.4rem" }}><StageBadge status={st} /></div>
                <div style={{ fontSize: "0.72em", color: "var(--color-muted-foreground)", marginTop: "0.3rem" }}>
                  {s.hint}
                </div>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// P&L summary — big numbers, colored, with tick animation
// ---------------------------------------------------------------------------

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
  const color = positiveColor
    ? value >= 0 ? "var(--color-success, #16a34a)" : "var(--color-destructive)"
    : "var(--color-foreground)";
  return (
    <div
      className="argus-num-tick"
      style={{
        fontSize: big ? "1.8em" : "1.1em",
        fontWeight: 700,
        color,
        transform: delta ? "scale(1.04)" : "scale(1.0)",
      }}
    >
      {fmtUsd(value)}
      {delta !== null && Math.abs(delta) > 0.001 && (
        <span style={{
          marginLeft: "0.5rem", fontSize: "0.55em", fontWeight: 500,
          color: delta > 0 ? "var(--color-success, #16a34a)" : "var(--color-destructive)",
        }}>
          {delta > 0 ? "+" : ""}{fmtUsd(delta).replace("$", "$")}
        </span>
      )}
    </div>
  );
}

function PnLSummary({ pnlData }) {
  const t = pnlData?.total || { revenue: 0, llm_cost: 0, external_spend: 0, pnl: 0 };
  const tiles = [
    { label: "Revenue",  value: t.revenue,        big: true },
    { label: "LLM cost (Nemotron)", value: t.llm_cost },
    { label: "External spend",      value: t.external_spend },
    { label: "P&L",      value: t.pnl, big: true, positiveColor: true },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Live P&amp;L</CardTitle>
      </CardHeader>
      <CardContent>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem" }}>
          {tiles.map((tile) => (
            <div key={tile.label}
              style={{
                padding: "1rem",
                borderRadius: "var(--radius)",
                border: "1px solid var(--color-border)",
              }}>
              <div style={{ fontSize: "0.8em", color: "var(--color-muted-foreground)" }}>{tile.label}</div>
              <AnimatedUsd value={tile.value} big={tile.big} positiveColor={tile.positiveColor} />
            </div>
          ))}
        </div>
        {pnlData?.jobs?.length > 0 && (
          <div style={{ marginTop: "1rem", fontSize: "0.85em", color: "var(--color-muted-foreground)" }}>
            {pnlData.jobs.map((j) => (
              <span key={j.job_id} style={{ marginRight: "1rem" }}>
                <code>{j.job_id}</code>: {fmtUsd(j.pnl)}
              </span>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Token vault — live count of unconsumed auth tokens
// ---------------------------------------------------------------------------

function TokenVault({ tokens }) {
  const items = tokens?.items || [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Active auth tokens ({items.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <p style={{ color: "var(--color-muted-foreground)", fontSize: "0.9em" }}>
            No active tokens. Each ALLOW issues a 60-second single-use token. Stripe spends without one are blocked.
          </p>
        ) : (
          <div style={{ display: "grid", gap: "0.4rem" }}>
            {items.map((t) => (
              <div key={t.token_preview} className="argus-slide-in"
                style={{
                  display: "flex", justifyContent: "space-between",
                  alignItems: "center", padding: "0.4rem 0.6rem",
                  border: "1px solid var(--color-border)",
                  borderRadius: "var(--radius)",
                  fontSize: "0.85em", fontFamily: "monospace",
                }}>
                <span><Badge>🔑 {t.token_preview}</Badge>  {t.cost_center_id} — {fmtUsd(t.amount_usd)}</span>
                <span style={{ color: "var(--color-muted-foreground)" }}>job <code>{t.job_id}</code></span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Approval queue — pulsing cards on insert
// ---------------------------------------------------------------------------

function ApprovalsSection() {
  const { data, error, refresh } = usePolling("/api/plugins/argus/approvals?status=pending", FAST_POLL_MS);
  const items = data?.items || [];
  const seenIdsRef = useRef(new Set());
  const [pulsing, setPulsing] = useState(new Set());

  useEffect(() => {
    const fresh = new Set();
    items.forEach((it) => {
      if (!seenIdsRef.current.has(it.id)) fresh.add(it.id);
      seenIdsRef.current.add(it.id);
    });
    if (fresh.size) {
      setPulsing(fresh);
      const t = setTimeout(() => setPulsing(new Set()), 2000);
      return () => clearTimeout(t);
    }
  }, [items]);

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
              <div key={it.id}
                className={pulsing.has(it.id) ? "argus-pulse argus-slide-in" : "argus-slide-in"}
                style={{
                  border: "1px solid var(--color-border)",
                  borderRadius: "var(--radius)",
                  padding: "0.75rem 1rem",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "1rem",
                  background: "var(--color-card)",
                }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: "1.05em" }}>
                    {fmtUsd(it.projected_usd)} — {it.cost_center_id}
                    {"  "}
                    <Badge variant={it.level === "finance" ? "destructive" : "secondary"}>{it.level}</Badge>
                  </div>
                  <div style={{ color: "var(--color-muted-foreground)", fontSize: "0.85em" }}>
                    job <code>{it.job_id}</code> · ref <code>{it.ref || "—"}</code> · {fmtTime(it.created_at)}
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

// ---------------------------------------------------------------------------
// Audit trail — color-coded badges, slide-in on new
// ---------------------------------------------------------------------------

const EVENT_TINT = {
  revenue_received:           { color: "var(--color-success, #16a34a)", label: "REVENUE" },
  spend_evaluated:            { color: "var(--color-muted-foreground)", label: "evaluated" },
  auth_token_issued:          { color: "var(--color-success, #16a34a)", label: "token" },
  approval_requested:         { color: "var(--color-warning, #f59e0b)", label: "needs approval" },
  approval_approved:          { color: "var(--color-success, #16a34a)", label: "APPROVED" },
  approval_rejected:          { color: "var(--color-destructive)",       label: "REJECTED" },
  spend_resumed:              { color: "var(--color-success, #16a34a)", label: "resumed" },
  spend_rejected:             { color: "var(--color-destructive)",       label: "blocked" },
  spend_timeout:              { color: "var(--color-destructive)",       label: "timeout" },
  stripe_blocked_no_token:    { color: "var(--color-destructive)",       label: "🚨 rogue blocked" },
  stripe_blocked_bad_token:   { color: "var(--color-destructive)",       label: "🚨 bad token" },
  stripe_blocked_no_amount:   { color: "var(--color-destructive)",       label: "🚨 no amount" },
  stripe_authorized:          { color: "var(--color-success, #16a34a)", label: "stripe ok" },
  refund_recorded:            { color: "var(--color-warning, #f59e0b)", label: "refund" },
  webhook_ignored:            { color: "var(--color-muted-foreground)", label: "ignored" },
  spend_skipped_missing_declaration: { color: "var(--color-muted-foreground)", label: "skipped" },
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
  return (
    <Card>
      <CardHeader>
        <CardTitle>Live event stream</CardTitle>
      </CardHeader>
      <CardContent>
        {error && <Badge variant="destructive">error: {error}</Badge>}
        {items.length === 0 ? (
          <p style={{ color: "var(--color-muted-foreground)" }}>No events yet — hit "Start commission" to begin.</p>
        ) : (
          <div style={{ maxHeight: "320px", overflow: "auto", fontSize: "0.85em" }}>
            {items.map((r, i) => {
              const tint = EVENT_TINT[r.event] || { color: "var(--color-muted-foreground)", label: r.event };
              const p = r.payload || {};
              const sig = r.ts + r.event;
              return (
                <div key={i} className={sig === newKey ? "argus-slide-in" : ""}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "100px 100px 1fr",
                    gap: "0.5rem",
                    padding: "0.3rem 0",
                    borderBottom: "1px solid var(--color-border)",
                    alignItems: "center",
                  }}>
                  <span style={{ color: "var(--color-muted-foreground)", fontFamily: "monospace" }}>{fmtTime(r.ts)}</span>
                  <span><Badge style={{ background: tint.color, color: "white" }}>{tint.label}</Badge></span>
                  <span style={{ fontFamily: "monospace", color: "var(--color-muted-foreground)" }}>
                    {p.cost_center_id && <span>{p.cost_center_id} </span>}
                    {p.projected_usd != null && <span>{fmtUsd(p.projected_usd)} </span>}
                    {p.amount_usd != null && <span>{fmtUsd(p.amount_usd)} </span>}
                    {p.reason && <span>· {p.reason}</span>}
                    {p.verdict && <span>· {p.verdict}</span>}
                    {p.ref && <span> · <code>{p.ref}</code></span>}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Start commission button — runs the whole Mermelada flow from the browser
// ---------------------------------------------------------------------------

function StartCommissionButton() {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const start = async () => {
    setRunning(true); setError(null);
    try {
      await SDK.fetchJSON("/api/plugins/argus/demo/mermelada/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  };
  const reset = async () => {
    if (!confirm("Wipe ledger + approvals + audit + tokens? (re-anchors latest Nemotron session)")) return;
    try {
      await SDK.fetchJSON("/api/plugins/argus/demo/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Start a Mermelada commission</CardTitle>
      </CardHeader>
      <CardContent>
        <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" }}>
          <Button onClick={start} disabled={running}>
            {running ? "⏳ Commission in progress — approve in queue above" : "▶ Start commission"}
          </Button>
          <Button variant="destructive" onClick={reset} disabled={running}>
            ↺ Reset demo
          </Button>
          <span style={{ color: "var(--color-muted-foreground)", fontSize: "0.85em" }}>
            Reset wipes the ledger and re-anchors the latest Nemotron session.
          </span>
        </div>
        {error && <Badge variant="destructive" style={{ marginTop: "0.5rem" }}>error: {error}</Badge>}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

function ArgusPage() {
  useAnimationsCSS();
  const { data: pnlData } = usePolling("/api/plugins/argus/pnl", POLL_MS);
  const { data: tokenData } = usePolling("/api/plugins/argus/tokens/active", FAST_POLL_MS);
  const { data: auditDataForStages } = usePolling("/api/plugins/argus/audit?limit=200", POLL_MS);
  // Stage statuses are derived from audit history (oldest-first scan).
  const stageStatuses = useMemo(() => {
    const items = (auditDataForStages?.items || []).slice().reverse();
    return deriveStageStatus(items);
  }, [auditDataForStages]);

  return (
    <div style={{ padding: "1.5rem", display: "grid", gap: "1rem" }}>
      <WorkflowTimeline statuses={stageStatuses} />
      <StartCommissionButton />
      <PnLSummary pnlData={pnlData} />
      <ApprovalsSection />
      <TokenVault tokens={tokenData} />
      <AuditSection />
    </div>
  );
}

window.__HERMES_PLUGINS__.register("argus", ArgusPage);
