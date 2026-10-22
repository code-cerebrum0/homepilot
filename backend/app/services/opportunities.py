from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Device, Reading
from app.schemas import OpportunityOut
from app.utils import local_midnight

NIGHT_START_H, NIGHT_END_H = 2, 5
NIGHTS = 7
STANDBY_THRESHOLD_W = 50.0
MIN_SAMPLES_PER_NIGHT = 6
MIN_NIGHTS_ON = 3


def _night_window(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    base = local_midnight(d, tz)
    return base + timedelta(hours=NIGHT_START_H), base + timedelta(hours=NIGHT_END_H)


def find_opportunities(db: Session, household_id: int, now: datetime, tz: ZoneInfo) -> list[OpportunityOut]:
    """Devices drawing meaningful power between 02:00-05:00 on most recent nights.

    Derived only from stored, non-simulated readings."""
    devices = db.execute(
        select(Device.id, Device.name).where(
            Device.household_id == household_id,
            Device.deleted_at.is_(None),
            Device.status == "active",
            Device.source != "simulated",
        )
    ).all()
    if not devices:
        return []
    names = {i: n for i, n in devices}

    today = now.astimezone(tz).date()
    nights: list[tuple[datetime, datetime]] = []
    for k in range(NIGHTS + 1):
        s, e = _night_window(today - timedelta(days=k), tz)
        if e <= now:
            nights.append((s, e))
        if len(nights) == NIGHTS:
            break

    stats: dict[int, dict] = {i: {"with_data": 0, "on": 0, "watts": []} for i in names}
    for s, e in nights:
        rows = db.execute(
            select(Reading.device_id, func.avg(Reading.power_w), func.count(Reading.id))
            .where(
                Reading.device_id.in_(names.keys()),
                Reading.measured_at >= s,
                Reading.measured_at < e,
                Reading.is_simulated.is_(False),
            )
            .group_by(Reading.device_id)
        ).all()
        for dev_id, avg_w, n in rows:
            if n < MIN_SAMPLES_PER_NIGHT:
                continue
            stats[dev_id]["with_data"] += 1
            if avg_w >= STANDBY_THRESHOLD_W:
                stats[dev_id]["on"] += 1
                stats[dev_id]["watts"].append(float(avg_w))

    out: list[OpportunityOut] = []
    hours = NIGHT_END_H - NIGHT_START_H
    for dev_id, st in stats.items():
        if st["on"] < MIN_NIGHTS_ON or st["on"] / st["with_data"] < 0.5:
            continue
        avg_w = sum(st["watts"]) / len(st["watts"])
        saving = avg_w / 1000 * hours * 30 * (st["on"] / st["with_data"])
        out.append(
            OpportunityOut(
                title=f"{names[dev_id]} runs overnight",
                detail=(
                    f"{names[dev_id]} averaged {avg_w:.0f} W between "
                    f"{NIGHT_START_H:02d}:00–{NIGHT_END_H:02d}:00 on {st['on']} of {st['with_data']} nights."
                ),
                estimated_monthly_saving_kwh=round(saving, 1),
            )
        )
    out.sort(key=lambda o: o.estimated_monthly_saving_kwh or 0, reverse=True)
    return out[:3]
