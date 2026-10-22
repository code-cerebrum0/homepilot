import calendar
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, Tariff
from app.schemas import CostOut, DailyPoint, DashboardOut, DeviceUsageOut, MonthOut
from app.services import costs, usage
from app.services.opportunities import find_opportunities
from app.utils import app_tz, local_midnight, utcnow
from app.config import get_settings

CURRENT_POWER_WINDOW = timedelta(minutes=10)


def _freshness(last: datetime | None, now: datetime) -> str:
    if last is None:
        return "none"
    age = max((now - last).total_seconds(), 0)
    if age <= 300:
        return "live"
    if age <= 3600:
        return "recent"
    return "stale"


def build_dashboard(db: Session, household_id: int, days: int, now: datetime | None = None) -> DashboardOut:
    settings = get_settings()
    now = now or utcnow()
    tz = app_tz()
    today = now.astimezone(tz).date()

    today_start = local_midnight(today, tz)
    tomorrow_start = local_midnight(today + timedelta(days=1), tz)
    yesterday_start = local_midnight(today - timedelta(days=1), tz)
    month_first = today.replace(day=1)
    month_start = local_midnight(month_first, tz)
    prev_first = (month_first - timedelta(days=1)).replace(day=1)
    prev_start = local_midnight(prev_first, tz)
    prev_end = min(prev_start + (now - month_start), month_start)  # same elapsed span, like-for-like

    # --- devices ---
    device_rows = db.scalars(
        select(Device).where(Device.household_id == household_id, Device.deleted_at.is_(None))
    ).all()
    per_device = usage.usage_by_device(db, household_id, today_start, tomorrow_start)
    today_kwh = sum(per_device.values())
    devices_out = [
        DeviceUsageOut(
            device_id=d.id,
            name=d.name,
            kwh_today=round(per_device.get(d.id, 0.0), 3),
            share_pct=round(per_device.get(d.id, 0.0) / today_kwh * 100, 1) if today_kwh > 0 else 0.0,
            is_simulated=d.source == "simulated",
            status=d.status,
            last_seen_at=d.last_seen_at,
        )
        for d in device_rows
    ]
    devices_out.sort(key=lambda x: x.kwh_today, reverse=True)

    # --- energy ---
    yesterday_kwh = usage.energy_between(db, household_id, yesterday_start, today_start)
    kwh_before_today = usage.energy_between(db, household_id, month_start, today_start)
    month_kwh = kwh_before_today + today_kwh
    prev_kwh = usage.energy_between(db, household_id, prev_start, prev_end)

    last = usage.last_reading_at(db, household_id)
    power = usage.current_power(db, household_id, now, CURRENT_POWER_WINDOW)

    # --- cost ---
    tariff = db.scalar(select(Tariff).where(Tariff.household_id == household_id))
    if tariff is None:
        cost = CostOut(today=0, month_to_date=0, currency=settings.default_currency, basis=costs.basis_text(None))
    else:
        rows_month = rows_before = None
        if tariff.type == "time_of_use":
            rows_month = usage.energy_rows(db, household_id, month_start, tomorrow_start)
            rows_before = [r for r in rows_month if r[0] < today_start]
        mtd_energy = costs.energy_charge(tariff, month_kwh, rows_month, tz)
        before_energy = costs.energy_charge(tariff, kwh_before_today, rows_before, tz)
        tax = 1 + tariff.tax_percent / 100
        dim = calendar.monthrange(today.year, today.month)[1]
        cost = CostOut(
            today=round(((mtd_energy - before_energy) + tariff.fixed_monthly_charge / dim) * tax, 2),
            month_to_date=round((mtd_energy + tariff.fixed_monthly_charge) * tax, 2),
            currency=tariff.currency,
            basis=costs.basis_text(tariff),
        )

    # --- daily series ---
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    daily = [DailyPoint(date=d, kwh=round(k, 3)) for d, k in zip(dates, usage.daily_series(db, household_id, dates, tz))]

    return DashboardOut(
        as_of=now,
        last_reading_at=last,
        freshness=_freshness(last, now),
        current_power_w=round(power, 1) if power is not None else None,
        today_kwh=round(today_kwh, 3),
        yesterday_kwh=round(yesterday_kwh, 3),
        cost=cost,
        month=MonthOut(kwh_this_period=round(month_kwh, 3), kwh_previous_period=round(prev_kwh, 3)),
        devices=devices_out,
        daily=daily,
        opportunities=find_opportunities(db, household_id, now, tz),
        has_simulated_data=any(d.is_simulated for d in devices_out),
    )
