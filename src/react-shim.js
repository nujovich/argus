// Provide React from the Hermes SDK so JSX runtime imports resolve without
// bundling React itself. esbuild's "automatic" JSX needs a React import in
// scope; this shim points it at window.__HERMES_PLUGIN_SDK__.React.

const React = window.__HERMES_PLUGIN_SDK__.React;
export { React };
export default React;
