import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401  (register tables)
from app.config import get_settings
from app.csrf import CSRFMiddleware
from app.db import Base, engine
from app.errors import register_error_handlers
from app.routers import auth, chat, dashboard, devices, readings, tariff, users
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

# Added first => runs inside CORS, so CSRF rejections still carry CORS headers.
app.add_middleware(CSRFMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

PREFIX = "/api/v1"
for r in (auth.router, users.router, dashboard.router, devices.router, readings.router, tariff.router, chat.router):
    app.include_router(r, prefix=PREFIX)


@app.get(f"{PREFIX}/health", tags=["health"])
def health():
    return {"status": "ok"}
