"""Assistant tools.

Every tool runs against the household id supplied by the server (ToolContext). The model can
never choose or override it: any argument not declared in a tool's schema (including
`household_id`) is rejected.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.services import usage
from app.services.dashboard import build_dashboard
from app.utils import iso_z, local_midnight


class ToolRejected(Exception):
    pass


@dataclass(frozen=True)
class ToolContext:
    db: Session
    household_id: int
    tz: ZoneInfo
    now: datetime

    @property
    def today(self) -> date:
        return self.now.astimezone(self.tz).date()

    def day(self, d: date) -> datetime:
        return local_midnight(d, self.tz)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], dict]
    summarize: Callable[[dict], str]

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _r(x: float, n: int = 3) -> float:
    return round(float(x), n)


def _pct(new: float, old: float) -> float | None:
    return None if old <= 0 else round((new - old) / old * 100, 1)


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def validate_args(schema: dict, args) -> dict:
    if not isinstance(args, dict):
        raise ToolRejected("arguments must be an object")
    props = schema.get("properties", {})
    extra = set(args) - set(props)
    if extra:
        raise ToolRejected(f"unexpected argument(s): {', '.join(sorted(extra))}")
    for req in schema.get("required", []):
        if req not in args:
            raise ToolRejected(f"missing argument: {req}")
    out = {}
    for key, value in args.items():
        spec = props[key]
        if spec.get("type") == "integer":
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolRejected(f"{key} must be an integer")
            if value < spec.get("minimum", value) or value > spec.get("maximum", value):
                raise ToolRejected(f"{key} must be between {spec.get('minimum')} and {spec.get('maximum')}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolRejected(f"{key} must be one of: {', '.join(spec['enum'])}")
        out[key] = value
    return out


# ---------------- handlers ----------------
def _current_usage(ctx: ToolContext, _: dict) -> dict:
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    if d.freshness == "none":
        return {"has_data": False, "message": "No readings have been recorded for this household yet."}
    return {
        "has_data": True,
        "current_power_w": d.current_power_w,
        "today_kwh_so_far": d.today_kwh,
        "last_reading_at": iso_z(d.last_reading_at),
        "freshness": d.freshness,
    }


def _daily_usage(ctx: ToolContext, a: dict) -> dict:
    days = a["days"]
    dates = [ctx.today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    values = usage.daily_series(ctx.db, ctx.household_id, dates, ctx.tz)
    total = sum(values)
    if total <= 0:
        return {"has_data": False, "days": days, "message": "No usage recorded in this period."}
    peak = max(range(len(values)), key=values.__getitem__)
    return {
        "has_data": True,
        "days": days,
        "total_kwh": _r(total),
        "average_kwh_per_day": _r(total / days),
        "highest_day": {"date": dates[peak].isoformat(), "kwh": _r(values[peak])},
        "note": "The final day (today) is partial.",
        "daily": [{"date": d.isoformat(), "kwh": _r(v)} for d, v in zip(dates, values)],
    }


def _compare(ctx: ToolContext, a: dict) -> dict:
    kind = a["comparison"]
    t = ctx.today
    note = None
    if kind == "today_vs_yesterday":
        cur = (ctx.day(t), ctx.day(t + timedelta(days=1)))
        prev = (ctx.day(t - timedelta(days=1)), ctx.day(t))
        labels = ("today (so far)", "yesterday (full day)")
        note = "Today is partial, so a lower figure is expected earlier in the day."
    elif kind == "this_week_vs_last_week":
        cur = (ctx.day(t - timedelta(days=6)), ctx.day(t + timedelta(days=1)))
        prev = (ctx.day(t - timedelta(days=13)), ctx.day(t - timedelta(days=6)))
        labels = ("last 7 days (incl. today)", "the 7 days before that")
        note = "Today is partial within the current 7-day window."
    else:
        first = t.replace(day=1)
        prev_first = (first - timedelta(days=1)).replace(day=1)
        cur_start = ctx.day(first)
        prev_start = ctx.day(prev_first)
        cur = (cur_start, ctx.day(t + timedelta(days=1)))
        prev = (prev_start, min(prev_start + (ctx.now - cur_start), cur_start))
        labels = ("this month so far", "same elapsed span of last month")
    cur_kwh = usage.energy_between(ctx.db, ctx.household_id, *cur)
    prev_kwh = usage.energy_between(ctx.db, ctx.household_id, *prev)
    if cur_kwh <= 0 and prev_kwh <= 0:
        return {"has_data": False, "message": "No readings recorded in either period."}
    out = {
        "has_data": True,
        "current": {"label": labels[0], "kwh": _r(cur_kwh)},
        "previous": {"label": labels[1], "kwh": _r(prev_kwh)},
        "difference_kwh": _r(cur_kwh - prev_kwh),
        "percent_change": _pct(cur_kwh, prev_kwh),
    }
    if out["percent_change"] is None:
        out["message"] = "The previous period has no usage, so a percentage change cannot be computed."
    if note:
        out["note"] = note
    return out


def _device_breakdown(ctx: ToolContext, a: dict) -> dict:
    t = ctx.today
    period = a["period"]
    start = {
        "today": ctx.day(t),
        "last_7_days": ctx.day(t - timedelta(days=6)),
        "this_month": ctx.day(t.replace(day=1)),
    }[period]
    per = usage.usage_by_device(ctx.db, ctx.household_id, start, ctx.day(t + timedelta(days=1)))
    from sqlalchemy import select

    from app.models import Device

    names = dict(
        ctx.db.execute(
            select(Device.id, Device.name).where(Device.household_id == ctx.household_id, Device.deleted_at.is_(None))
        ).all()
    )
    total = sum(v for k, v in per.items() if k in names)
    if total <= 0:
        return {"has_data": False, "period": period, "message": "No device usage recorded in this period."}
    rows = sorted(((names[k], v) for k, v in per.items() if k in names), key=lambda x: x[1], reverse=True)
    return {
        "has_data": True,
        "period": period,
        "total_kwh": _r(total),
        "devices": [{"name": n, "kwh": _r(v), "share_pct": round(v / total * 100, 1)} for n, v in rows],
    }


def _cost(ctx: ToolContext, _: dict) -> dict:
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    c = d.cost
    if c.basis == "No tariff configured":
        return {"has_data": False, "message": "No tariff is configured, so cost cannot be estimated."}
    return {
        "has_data": True,
        "is_estimate": True,
        "currency": c.currency,
        "cost_today": c.today,
        "cost_month_to_date": c.month_to_date,
        "basis": c.basis,
        "kwh_month_to_date": d.month.kwh_this_period,
    }


def _opportunities(ctx: ToolContext, _: dict) -> dict:
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    if not d.opportunities:
        return {"has_data": False, "message": "No savings opportunities were found in the recorded readings."}
    return {"has_data": True, "opportunities": [o.model_dump() for o in d.opportunities]}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "get_current_usage",
            "Get the household's current power draw (watts) and energy used so far today (kWh).",
            _empty_schema(),
            _current_usage,
            lambda a: "current power and today's usage",
        ),
        Tool(
            "get_daily_usage",
            "Get energy use (kWh) per day for the last N days, with total, average and highest day.",
            {
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "Number of days"}},
                "required": ["days"],
                "additionalProperties": False,
            },
            _daily_usage,
            lambda a: f"last {a.get('days')} days",
        ),
        Tool(
            "compare_energy_usage",
            "Compare energy use between two periods and return the difference and percent change.",
            {
                "type": "object",
                "properties": {
                    "comparison": {
                        "type": "string",
                        "enum": ["today_vs_yesterday", "this_week_vs_last_week", "this_month_vs_last_month"],
                    }
                },
                "required": ["comparison"],
                "additionalProperties": False,
            },
            _compare,
            lambda a: str(a.get("comparison", "")).replace("_", " "),
        ),
        Tool(
            "get_device_breakdown",
            "Get energy use per device (kWh and share of total) for a period.",
            {
                "type": "object",
                "properties": {"period": {"type": "string", "enum": ["today", "last_7_days", "this_month"]}},
                "required": ["period"],
                "additionalProperties": False,
            },
            _device_breakdown,
            lambda a: f"devices, {str(a.get('period', '')).replace('_', ' ')}",
        ),
        Tool(
            "get_cost_estimate",
            "Get the estimated electricity cost for today and the month to date, using the configured tariff.",
            _empty_schema(),
            _cost,
            lambda a: "cost today and month to date",
        ),
        Tool(
            "get_savings_opportunities",
            "Get energy saving opportunities detected from the recorded readings.",
            _empty_schema(),
            _opportunities,
            lambda a: "savings opportunities",
        ),
    ]
}

TOOL_SPECS = [t.spec() for t in TOOLS.values()]
