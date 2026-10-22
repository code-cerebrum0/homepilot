from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import Device, Reading
from app.utils import local_midnight


def _household_filter(household_id: int):
    return (Device.household_id == household_id, Device.deleted_at.is_(None))


def _readings_join(q):
    return q.select_from(Reading).join(Device, Device.id == Reading.device_id)


def energy_between(db: Session, household_id: int, start: datetime, end: datetime | None = None) -> float:
    q = _readings_join(select(func.coalesce(func.sum(Reading.energy_kwh), 0.0))).where(
        *_household_filter(household_id), Reading.measured_at >= start
    )
    if end is not None:
        q = q.where(Reading.measured_at < end)
    return float(db.scalar(q) or 0.0)


def energy_rows(db: Session, household_id: int, start: datetime, end: datetime | None) -> list[tuple[datetime, float]]:
    q = _readings_join(select(Reading.measured_at, Reading.energy_kwh)).where(
        *_household_filter(household_id), Reading.measured_at >= start
    )
    if end is not None:
        q = q.where(Reading.measured_at < end)
    return [(m, float(k)) for m, k in db.execute(q).all()]


def usage_by_device(db: Session, household_id: int, start: datetime, end: datetime) -> dict[int, float]:
    q = (
        _readings_join(select(Reading.device_id, func.coalesce(func.sum(Reading.energy_kwh), 0.0)))
        .where(*_household_filter(household_id), Reading.measured_at >= start, Reading.measured_at < end)
        .group_by(Reading.device_id)
    )
    return {d: float(k) for d, k in db.execute(q).all()}


def daily_series(db: Session, household_id: int, dates: list[date], tz: ZoneInfo) -> list[float]:
    """kWh per local calendar day, in one query (DST-safe: boundaries computed per day)."""
    bounds = [(local_midnight(d, tz), local_midnight(d + timedelta(days=1), tz)) for d in dates]
    cols = [
        func.coalesce(
            func.sum(case((and_(Reading.measured_at >= s, Reading.measured_at < e), Reading.energy_kwh), else_=0.0)),
            0.0,
        ).label(f"d{i}")
        for i, (s, e) in enumerate(bounds)
    ]
    q = _readings_join(select(*cols)).where(
        *_household_filter(household_id),
        Reading.measured_at >= bounds[0][0],
        Reading.measured_at < bounds[-1][1],
    )
    row = db.execute(q).one()
    return [float(v or 0.0) for v in row]


def last_reading_at(db: Session, household_id: int) -> datetime | None:
    q = _readings_join(select(func.max(Reading.measured_at))).where(*_household_filter(household_id))
    return db.scalar(q)


def current_power(db: Session, household_id: int, now: datetime, window: timedelta) -> float | None:
    """Sum of each active device's latest reading within `window`; None if there is none."""
    latest = (
        _readings_join(select(Reading.device_id.label("device_id"), func.max(Reading.measured_at).label("m")))
        .where(*_household_filter(household_id), Device.status == "active", Reading.measured_at >= now - window)
        .group_by(Reading.device_id)
        .subquery()
    )
    q = (
        select(Reading.device_id, func.max(Reading.power_w))  # max() guards against same-instant duplicates
        .join(latest, and_(Reading.device_id == latest.c.device_id, Reading.measured_at == latest.c.m))
        .group_by(Reading.device_id)
    )
    rows = db.execute(q).all()
    return float(sum(p for _, p in rows)) if rows else None
