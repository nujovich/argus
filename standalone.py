"""Argus standalone ASGI app — serve the SPA + API from ONE process/origin.

Inside Hermes the plugin's router is mounted under `/api/plugins/argus` by the
host, the dashboard is served by Hermes, and the Bearer session token is enforced
by the host. Outside Hermes none of that exists, so this module supplies it:

  - its own FastAPI app with the router mounted under `/api/plugins/argus`
    (outside Hermes nothing else supplies the prefix);
  - the built SPA served SAME-ORIGIN at "/" via StaticFiles(html=True) — the
    canonical demo setup, so no CORS is needed in practice;
  - permissive localhost CORS anyway, as a dev safety net (e.g. a Vite dev server
    on :5173 hitting this API during development);
  - OPTIONAL Bearer auth: OFF by default for the local demo, but if the env var
    ``ARGUS_DASHBOARD_TOKEN`` is set it is required on the API routes.

This is backend glue only — no ledger / policy / enforcement / gating code lives
here. Run with: ``python -m standalone`` (or ``python standalone.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Make flat imports (db, config, dashboard.plugin_api) work when run directly.
_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from dashboard import plugin_api  # noqa: E402

API_PREFIX = "/api/plugins/argus"
STATIC_DIR = _DIR / "dashboard" / "static"
# Permissive for local dev: any localhost / 127.0.0.1 port (e.g. Vite :5173).
_CORS_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

_DASHBOARD_TOKEN_ENV = "ARGUS_DASHBOARD_TOKEN"


def build_app() -> FastAPI:
    """Construct the standalone app: optional-auth middleware + permissive CORS,
    the API router under the Hermes prefix, and the SPA served same-origin."""
    app = FastAPI(title="Argus (standalone)")

    # ── optional Bearer auth (inner middleware) ──────────────────────────────
    # Read the token PER REQUEST so the flag reflects the current environment.
    # Only the API routes are gated; the static SPA at "/" is always reachable
    # (the SPA itself attaches the Bearer to its API calls). Preflight OPTIONS
    # is never gated so CORS can answer it.
    @app.middleware("http")
    async def _optional_auth(request, call_next):
        token = os.environ.get(_DASHBOARD_TOKEN_ENV)
        if (
            token
            and request.method != "OPTIONS"
            and request.url.path.startswith(API_PREFIX)
        ):
            if request.headers.get("authorization", "") != f"Bearer {token}":
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ── CORS (added last → outermost, so it also answers preflight cleanly) ──
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_CORS_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── API under the Hermes prefix (must register BEFORE the "/" static mount
    #    so its routes win over the catch-all) ─────────────────────────────────
    app.include_router(plugin_api.router, prefix=API_PREFIX)

    # ── SPA served same-origin at "/" (placeholder until the real SPA lands) ──
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="spa")

    return app


app = build_app()


def main() -> int:
    import uvicorn  # local import: only needed to actually serve

    host = os.environ.get("ARGUS_HOST", "127.0.0.1")
    port = int(os.environ.get("ARGUS_PORT", "9119"))
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
