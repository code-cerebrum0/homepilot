"""Optional background generator of readings for devices whose source is 'simulated'."""
import asyncio
import logging
import math
import random

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Device, Reading
from app.utils import app_tz, utcnow

log = logging.getLogger("homepilot.simulator")


def _power_for(device_id: int, now) -> float:
    local = now.astimezone(app_tz())
    hour = local.hour + local.minute / 60
    daily = 1 + math.sin(2 * math.pi * (hour - 8) / 24)        # peaks late afternoon
    evening = 120 if 18 <= hour <= 23 else 0
    base = 90 + (device_id % 5) * 60
    return max(0.0, base + 80 * daily + evening + random.uniform(-20, 20))


def tick() -> int:
    interval = get_settings().simulator_interval_seconds
    now = utcnow()
    bucket = int(now.timestamp()) // interval
    with SessionLocal() as db:
        devices = db.scalars(
            select(Device).where(
                Device.source == "simulated", Device.status == "active", Device.deleted_at.is_(None)
            )
        ).all()
        for d in devices:
            power = round(_power_for(d.id, now), 1)
            key = f"sim-{d.id}-{bucket}"
            if db.scalar(select(Reading.id).where(Reading.device_id == d.id, Reading.idempotency_key == key)):
                continue
            db.add(
                Reading(
                    device_id=d.id,
                    measured_at=now,
                    ingested_at=now,
                    power_w=power,
                    energy_kwh=power * interval / 3.6e6,
                    interval_seconds=interval,
                    is_simulated=True,
                    idempotency_key=key,
                )
            )
            d.last_seen_at = now
        db.commit()
        return len(devices)


async def run_simulator(stop: asyncio.Event) -> None:
    interval = get_settings().simulator_interval_seconds
    log.info("Simulator started (every %ss)", interval)
    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("Simulator tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
