from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import Tariff


def slab_cost(slabs: list[dict], kwh: float) -> float:
    cost, prev = 0.0, 0.0
    for s in slabs:
        upper = s.get("up_to_kwh")
        rate = float(s["rate_per_kwh"])
        if upper is None or kwh <= upper:
            return cost + max(kwh - prev, 0.0) * rate
        cost += (float(upper) - prev) * rate
        prev = float(upper)
    return cost


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def tou_rate(periods: list[dict], fallback: float, local_dt: datetime) -> float:
    t = local_dt.hour * 60 + local_dt.minute
    for p in periods:
        s, e = _minutes(p["start"]), _minutes(p["end"])
        inside = s <= t < e if s < e else (t >= s or t < e)  # wraps midnight when s > e
        if inside:
            return float(p["rate_per_kwh"])
    return fallback


def energy_charge(
    tariff: Tariff, kwh: float, rows: list[tuple[datetime, float]] | None, tz: ZoneInfo
) -> float:
    """Energy-only charge (no fixed charge, no tax) for `kwh` consumed in a billing month.

    For time-of-use tariffs `rows` (measured_at, kWh) must be supplied; hours not covered by any
    period are billed at flat_rate_per_kwh (or 0 if unset)."""
    flat = float(tariff.flat_rate_per_kwh or 0.0)
    if tariff.type == "slab":
        return slab_cost(tariff.slabs or [], kwh)
    if tariff.type == "time_of_use":
        periods = tariff.tou_periods or []
        return sum(k * tou_rate(periods, flat, m.astimezone(tz)) for m, k in (rows or []))
    return kwh * flat


def basis_text(tariff: Tariff | None) -> str:
    if tariff is None:
        return "No tariff configured"
    kind = {
        "flat": f"flat rate {float(tariff.flat_rate_per_kwh or 0):.2f}/kWh",
        "slab": "slab rates",
        "time_of_use": "time-of-use rates",
    }[tariff.type]
    parts = [kind]
    if tariff.fixed_monthly_charge:
        parts.append("fixed charge")
    if tariff.tax_percent:
        parts.append(f"{tariff.tax_percent:g}% tax")
    return " + ".join(parts)
