// Argus dashboard plugin — esbuild entry.
//
// Bundles src/index.jsx into a single IIFE at dashboard/dist/index.js.
// React is NOT bundled — the plugin reads it from window.__HERMES_PLUGIN_SDK__
// at runtime (see CLAUDE.md §7).

import { build } from "esbuild";

const watch = process.argv.includes("--watch");

const opts = {
  entryPoints: ["src/index.jsx"],
  outfile: "dashboard/dist/index.js",
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2020"],
  // Classic JSX runtime — JSX compiles to React.createElement(...) calls,
  // and React is provided via the inject shim that pulls it off
  // window.__HERMES_PLUGIN_SDK__. This avoids any "react/jsx-runtime"
  // resolution and means React itself is never bundled.
  jsx: "transform",
  inject: ["src/react-shim.js"],
  loader: { ".js": "jsx" },
  logLevel: "info",
  minify: false,
  sourcemap: false,
};

if (watch) {
  const ctx = await (await import("esbuild")).context(opts);
  await ctx.watch();
  console.log("argus: esbuild watching…");
} else {
  await build(opts);
}
