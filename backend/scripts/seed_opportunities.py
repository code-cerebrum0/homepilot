"""Seed overnight-standby readings so the savings-opportunity analysis fires.

Run from the backend/ directory:
    python scripts/seed_opportunities.py

What it does
------------
1. Finds the first household in the DB (or creates one if none exists).
2. For each device in DEVICES_TO_CREATE (if not already present) creates a
   consumer device with source="smart_plug".
3. Injects 9 readings per night between 02:00–04:40 IST for the last 5 nights.
   At the configured wattages these will all exceed the 50 W standby threshold.

After running:
  - Ask the assistant "What can I do to reduce my electricity bill?"
  - get_savings_opportunities will return status "ok" with 1–3 opportunities.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ── locate the backend package ────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)          # backend/
if _root not in sys.path:
    sys.path.insert(0, _root)

os.environ.setdefault("AUTO_CREATE_TABLES", "true")

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.models import Device, Household, Membership, Reading, User
from app.security import hash_password
from app.utils import local_midnight

# ── What to seed ─────────────────────────────────────────────────────────────

DEVICES_TO_CREATE = [
    {"name": "Water Heater",  "avg_overnight_w": 1800.0},   # geyser left on
    {"name": "Air Conditioner", "avg_overnight_w": 1500.0}, # AC running all night
    {"name": "TV / Set-top Box", "avg_overnight_w": 80.0},  # standby draw
]

NIGHTS = 5          # seed readings for the last 5 nights
INTERVAL_S = 1200   # 20-minute interval (matches MIN_SAMPLES_PER_NIGHT=6 requirement)


def _energy(power_w: float, interval_s: int) -> float:
    """Convert W + seconds → kWh (exact formula the API validates against)."""
    return round(power_w * interval_s / 3_600_000, 6)


def seed():
    settings = get_settings()
    tz = ZoneInfo(settings.app_timezone)
    Base.metadata.create_all(engine)

    with SessionLocal() as db:
        # ── 1. Find or create a demo user + household ──────────────────────────
        user = db.query(User).first()
        if user is None:
            print("No users found. Creating demo user demo@homepilot.local / demo1234567")
            import secrets
            user = User(
                name="Demo User",
                email="demo@homepilot.local",
                password_hash=hash_password("demo1234567"),
            )
            db.add(user)
            db.flush()

        hh = (
            db.query(Household)
            .join(Membership, Membership.household_id == Household.id)
            .filter(Membership.user_id == user.id)
            .first()
        )
        if hh is None:
            import secrets as _s
            hh = Household(name="Demo Home")
            db.add(hh)
            db.flush()
            db.add(Membership(user_id=user.id, household_id=hh.id, role="owner"))
            db.flush()

        print(f"Using household #{hh.id} '{hh.name}' for user '{user.email}'")

        # ── 2. Ensure consumer devices exist ──────────────────────────────────
        devices_and_watts: list[tuple[Device, float]] = []
        import secrets as _s

        for spec in DEVICES_TO_CREATE:
            # Look for an existing smart_plug (non-simulated) device with this name.
            # Simulated devices are excluded by find_opportunities, so we never reuse them.
            dev = db.query(Device).filter_by(
                household_id=hh.id,
                name=spec["name"],
                source="smart_plug",
            ).first()
            if dev is None:
                dev = Device(
                    household_id=hh.id,
                    name=spec["name"],
                    source="smart_plug",   # NOT "simulated" so analysis includes it
                    kind="consumer",
                    status="active",
                    secret_hash=_s.token_hex(32),
                )
                db.add(dev)
                db.flush()
                print(f"  Created device: {dev.name} (id={dev.id}, source=smart_plug)")
            else:
                print(f"  Found existing smart_plug device: {dev.name} (id={dev.id})")
            devices_and_watts.append((dev, spec["avg_overnight_w"]))

        # ── 3. Inject overnight readings ──────────────────────────────────────
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz)
        readings_added = 0

        for dev, power_w in devices_and_watts:
            for night_offset in range(NIGHTS):
                local_date = now_local.date() - timedelta(days=night_offset + 1)
                # 02:00 local on that date (IST-aware)
                local_2am = datetime(
                    local_date.year, local_date.month, local_date.day,
                    2, 0, 0, tzinfo=tz
                )
                # 9 readings at INTERVAL_S apart → covers 02:00–04:40 local
                for i in range(9):
                    ts = local_2am + timedelta(seconds=i * INTERVAL_S)
                    ikey = f"seed_opp_{dev.id}_{night_offset}_{i}"

                    # Skip if already seeded (idempotent)
                    exists = db.query(Reading).filter_by(
                        device_id=dev.id, idempotency_key=ikey
                    ).first()
                    if exists:
                        continue

                    db.add(Reading(
                        device_id=dev.id,
                        measured_at=ts,
                        power_w=power_w,
                        energy_kwh=_energy(power_w, INTERVAL_S),
                        interval_seconds=INTERVAL_S,
                        is_simulated=False,
                        idempotency_key=ikey,
                    ))
                    readings_added += 1

        db.commit()
        print(f"\nDone. Added {readings_added} new readings across {len(devices_and_watts)} devices.")
        print(f"Nights seeded: last {NIGHTS} nights between 02:00–04:40 {settings.app_timezone}.")
        print("\nNow ask the assistant:")
        print('  "What can I do to reduce my electricity bill?"')
        print('  "Check for overnight standby waste"')
        print('  "Which devices are costing me money overnight?"')


if __name__ == "__main__":
    seed()
