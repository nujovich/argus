(() => {
  // src/react-shim.js
  var React = window.__HERMES_PLUGIN_SDK__.React;

  // src/index.jsx
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var { React: React2 } = SDK;
  var { useEffect, useState } = SDK.hooks;
  var { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  function ArgusPage() {
    const [health, setHealth] = useState(null);
    const [error, setError] = useState(null);
    useEffect(() => {
      SDK.fetchJSON("/api/plugins/argus/health").then(setHealth).catch((err) => setError(String(err)));
    }, []);
    return /* @__PURE__ */ React2.createElement("div", { style: { padding: "1.5rem", display: "grid", gap: "1rem" } }, /* @__PURE__ */ React2.createElement(Card, null, /* @__PURE__ */ React2.createElement(CardHeader, null, /* @__PURE__ */ React2.createElement(CardTitle, null, "Argus \u2014 scaffold OK")), /* @__PURE__ */ React2.createElement(CardContent, null, /* @__PURE__ */ React2.createElement("p", { style: { color: "var(--color-muted-foreground)" } }, "Horizontal financial control plane for money-spending agents. This is the Phase 2 placeholder; the real P&L, approval queue, and audit trail land next."), /* @__PURE__ */ React2.createElement("div", { style: { marginTop: "1rem", display: "flex", gap: "0.5rem", alignItems: "center" } }, /* @__PURE__ */ React2.createElement("span", null, "Backend:"), error ? /* @__PURE__ */ React2.createElement(Badge, { variant: "destructive" }, "error: ", error) : health ? /* @__PURE__ */ React2.createElement(Badge, null, health.plugin, " v", health.version, " \u2014 ", health.status) : /* @__PURE__ */ React2.createElement(Badge, { variant: "secondary" }, "loading\u2026")))));
  }
  window.__HERMES_PLUGINS__.register("argus", ArgusPage);
})();
