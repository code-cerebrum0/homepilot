from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import Device, Reading
from app.utils import local_midnight

CONSUMER = "consumer"
GENERATOR = "generator"


def _household_filter(household_id: int):
    return (Device.household_id == household_id, Device.deleted_at.is_(None))


def _readings_join(q):
    return q.select_from(Reading).join(Device, Device.id == Reading.device_id)


def energy_between(
    db: Session,
    household_id: int,
    start: datetime,
    end: datetime | None = None,
    kind: str | None = None,
) -> float:
    """Total kWh between start and end. Pass kind='consumer' or kind='generator' to filter."""
    q = _readings_join(select(func.coalesce(func.sum(Reading.energy_kwh), 0.0))).where(
        *_household_filter(household_id), Reading.measured_at >= start
    )
    if end is not None:
        q = q.where(Reading.measured_at < end)
    if kind is not None:
        q = q.where(Device.kind == kind)
    return float(db.scalar(q) or 0.0)


def energy_rows(
    db: Session,
    household_id: int,
    start: datetime,
    end: datetime | None,
    kind: str | None = None,
) -> list[tuple[datetime, float]]:
    q = _readings_join(select(Reading.measured_at, Reading.energy_kwh)).where(
        *_household_filter(household_id), Reading.measured_at >= start
    )
    if end is not None:
        q = q.where(Reading.measured_at < end)
    if kind is not None:
        q = q.where(Device.kind == kind)
    return [(m, float(k)) for m, k in db.execute(q).all()]


def usage_by_device(db: Session, household_id: int, start: datetime, end: datetime) -> dict[int, float]:
    q = (
        _readings_join(select(Reading.device_id, func.coalesce(func.sum(Reading.energy_kwh), 0.0)))
        .where(*_household_filter(household_id), Reading.measured_at >= start, Reading.measured_at < end)
        .group_by(Reading.device_id)
    )
    return {d: float(k) for d, k in db.execute(q).all()}


def daily_series(
    db: Session,
    household_id: int,
    dates: list[date],
    tz: ZoneInfo,
    kind: str | None = None,
) -> list[float]:
    """kWh per local calendar day, in one query (DST-safe). Filter by kind if provided."""
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
    if kind is not None:
        q = q.where(Device.kind == kind)
    row = db.execute(q).one()
    return [float(v or 0.0) for v in row]


def last_reading_at(db: Session, household_id: int) -> datetime | None:
    q = _readings_join(select(func.max(Reading.measured_at))).where(*_household_filter(household_id))
    return db.scalar(q)


def current_power(
    db: Session,
    household_id: int,
    now: datetime,
    window: timedelta,
    kind: str | None = None,
) -> float | None:
    """Sum of each active device's latest reading within `window`. Filter by kind if provided."""
    q = _readings_join(select(Reading.device_id.label("device_id"), func.max(Reading.measured_at).label("m"))).where(
        *_household_filter(household_id),
        Device.status == "active",
        Reading.measured_at >= now - window,
    )
    if kind is not None:
        q = q.where(Device.kind == kind)
    latest = q.group_by(Reading.device_id).subquery()
    rows = db.execute(
        select(Reading.device_id, func.max(Reading.power_w))
        .join(latest, and_(Reading.device_id == latest.c.device_id, Reading.measured_at == latest.c.m))
        .group_by(Reading.device_id)
    ).all()
    return float(sum(p for _, p in rows)) if rows else None
