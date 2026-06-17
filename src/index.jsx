/* Argus dashboard plugin — scaffold entry.
 *
 * Reads the Hermes plugin SDK from window.__HERMES_PLUGIN_SDK__ (never
 * bundles React) and registers a single placeholder component that proves
 * front-end ↔ back-end wiring by calling /api/plugins/argus/health.
 *
 * Real UI (P&L, approval queue, audit trail) lands in Phase 3.
 */

const SDK = window.__HERMES_PLUGIN_SDK__;
const { React } = SDK;
const { useEffect, useState } = SDK.hooks;
const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;

function ArgusPage() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    SDK.fetchJSON("/api/plugins/argus/health")
      .then(setHealth)
      .catch((err) => setError(String(err)));
  }, []);

  return (
    <div style={{ padding: "1.5rem", display: "grid", gap: "1rem" }}>
      <Card>
        <CardHeader>
          <CardTitle>Argus — scaffold OK</CardTitle>
        </CardHeader>
        <CardContent>
          <p style={{ color: "var(--color-muted-foreground)" }}>
            Horizontal financial control plane for money-spending agents.
            This is the Phase 2 placeholder; the real P&amp;L, approval
            queue, and audit trail land next.
          </p>
          <div style={{ marginTop: "1rem", display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <span>Backend:</span>
            {error ? (
              <Badge variant="destructive">error: {error}</Badge>
            ) : health ? (
              <Badge>
                {health.plugin} v{health.version} — {health.status}
              </Badge>
            ) : (
              <Badge variant="secondary">loading…</Badge>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

window.__HERMES_PLUGINS__.register("argus", ArgusPage);
