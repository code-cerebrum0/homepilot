import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app import models  # noqa: F401  (register tables)
from app.config import get_settings
from app.csrf import CSRFMiddleware
from app.db import Base, engine
from app.errors import register_error_handlers
from app.routers import auth, chat, dashboard, devices, readings, tariff, users
from app.routers import mcp as mcp_router
from app.routers import oauth as oauth_router
from app.services.simulator import run_simulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    stop = asyncio.Event()
    task = asyncio.create_task(run_simulator(stop)) if settings.simulator_enabled else None
    yield
    stop.set()
    if task:
        await task


app = FastAPI(title="HomePilot API", version="1.0.0", lifespan=lifespan)
register_error_handlers(app)

# ─── CSRF middleware (runs innermost, after CORS) ──────────────────────────────
# Added first => runs inside CORS, so CSRF rejections still carry CORS headers.
app.add_middleware(CSRFMiddleware)

# ─── Existing credentialed CORS for /api/v1 ──────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

# ─── Separate CORS for MCP + OAuth + well-known (no credentials) ─────────────
# These paths use Bearer tokens, so no cookies / credentials needed.
_MCP_CORS_ORIGINS = list(settings.mcp_allowed_origins_set)
_MCP_CORS_PATHS = ("/mcp/", "/oauth/", "/.well-known/")


class _McpCorsMiddleware(BaseHTTPMiddleware):
    """
    Lightweight CORS middleware for /mcp, /oauth/* and /.well-known/* paths.
    - Allow origins from mcp_allowed_origins_set (no credentials).
    - Reflect requested headers.
    - Expose WWW-Authenticate.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not any(path.startswith(p) for p in _MCP_CORS_PATHS):
            return await call_next(request)

        origin = request.headers.get("origin", "")
        allowed_origins = settings.mcp_allowed_origins_set

        # Preflight
        if request.method == "OPTIONS":
            resp = Response(status_code=204)
        else:
            resp = await call_next(request)

        if origin.rstrip("/") in {o.rstrip("/") for o in allowed_origins}:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Methods"] = "POST, GET, DELETE, OPTIONS"
            req_headers = request.headers.get("access-control-request-headers", "")
            resp.headers["Access-Control-Allow-Headers"] = req_headers or "Content-Type, Authorization, MCP-Protocol-Version, Mcp-Method, Mcp-Name"
            resp.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate"
            # Never include credentials
        return resp


app.add_middleware(_McpCorsMiddleware)

# ─── Routers ──────────────────────────────────────────────────────────────────

PREFIX = "/api/v1"
for r in (auth.router, users.router, dashboard.router, devices.router, readings.router, tariff.router, chat.router):
    app.include_router(r, prefix=PREFIX)

# MCP + OAuth at site root (no prefix)
app.include_router(mcp_router.router)
app.include_router(oauth_router.router)


@app.get(f"{PREFIX}/health", tags=["health"])
def health():
    return {"status": "ok"}
