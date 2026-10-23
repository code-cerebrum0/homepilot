"""MCP tool definitions for HomePilot energy assistant.

Tools are read-only, household-scoped, and voice-ready.
The household_id is injected server-side; any attempt to pass it as an
argument is rejected with isError:true (never a protocol error).
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device
from app.services import usage
from app.services.dashboard import build_dashboard
from app.utils import iso_z, local_midnight


# ─── Context & errors ────────────────────────────────────────────────────────

class ToolRejected(Exception):
    """Argument validation failure — returned as isError:true, not a protocol error."""


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


# ─── Tool descriptor ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class McpTool:
    name: str
    title: str
    description: str
    input_schema: dict          # strict JSON Schema (additionalProperties:false)
    handler: Callable[[ToolContext, dict], dict]
    label_fn: Callable[[dict], str]   # returns short label for chat summary

    def mcp_spec(self) -> dict:
        """MCP tools/list entry (2026-07-28 shape)."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }

    def ollama_spec(self) -> dict:
        """Ollama/OpenAI-compatible tool spec for chat_completion."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _r(x: float, n: int = 1) -> float:
    return round(float(x), n)


def _pct(new: float, old: float) -> float | None:
    return None if old <= 0 else round((new - old) / old * 100, 1)


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def validate_args(schema: dict, args: object) -> dict:
    """Validate args against the tool's inputSchema. Raises ToolRejected on any error."""
    if not isinstance(args, dict):
        raise ToolRejected("arguments must be an object")
    props = schema.get("properties", {})
    extra = set(args) - set(props)
    if extra:
        raise ToolRejected(f"unexpected argument(s): {', '.join(sorted(extra))}")
    for req in schema.get("required", []):
        if req not in args:
            raise ToolRejected(f"missing required argument: {req}")
    out: dict = {}
    for key, value in args.items():
        spec = props[key]
        if spec.get("type") == "integer":
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolRejected(f"{key} must be an integer")
            lo = spec.get("minimum", value)
            hi = spec.get("maximum", value)
            if value < lo or value > hi:
                raise ToolRejected(f"{key} must be between {lo} and {hi}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolRejected(f"{key} must be one of: {', '.join(str(v) for v in spec['enum'])}")
        out[key] = value
    return out


# ─── Handlers ────────────────────────────────────────────────────────────────

def _current_usage(ctx: ToolContext, _: dict) -> dict:
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    if d.freshness == "none":
        return {"has_data": False}
    result: dict = {
        "has_data": True,
        "current_power_w": d.current_power_w,
        "today_kwh_so_far": _r(d.today_kwh),
        "last_reading_at": iso_z(d.last_reading_at),
        "freshness": d.freshness,
    }
    if d.has_solar:
        result["solar_current_w"] = d.solar_current_w
        result["solar_today_kwh"] = _r(d.solar_today_kwh)
        result["net_today_kwh"] = _r(d.net_today_kwh)
    return result


def _current_usage_text(data: dict) -> str:
    if not data.get("has_data"):
        return "No readings have been recorded for this household yet."
    pw = data.get("current_power_w")
    kwh = data.get("today_kwh_so_far", 0)
    power_str = f"{_r(pw, 0):.0f} watts" if pw is not None else "unknown watts"
    base = (
        f"Your home is consuming {power_str} right now. "
        f"So far today you've used {kwh:.1f} kilowatt-hours."
    )
    if "solar_today_kwh" in data:
        sw = data.get("solar_current_w")
        solar_kwh = data.get("solar_today_kwh", 0)
        net = data.get("net_today_kwh", 0)
        solar_str = f"{_r(sw, 0):.0f} watts" if sw is not None else "unknown watts"
        base += (
            f" Your solar panels are generating {solar_str} right now "
            f"and have produced {solar_kwh:.1f} kilowatt-hours today. "
            f"Net grid draw today is {net:.1f} kilowatt-hours."
        )
    return base


def _daily_usage(ctx: ToolContext, a: dict) -> dict:
    days = a["days"]
    dates = [ctx.today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    values = usage.daily_series(ctx.db, ctx.household_id, dates, ctx.tz)
    total = sum(values)
    if total <= 0:
        return {"has_data": False, "days": days}
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


def _daily_usage_text(data: dict) -> str:
    if not data.get("has_data"):
        return f"No usage was recorded in the last {data.get('days', '?')} days."
    days = data["days"]
    total = data["total_kwh"]
    avg = data["average_kwh_per_day"]
    return (
        f"Over the last {days} days you used {total:.1f} kilowatt-hours in total, "
        f"averaging {avg:.1f} kilowatt-hours per day."
    )


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
        labels = ("last 7 days (including today)", "the 7 days before that")
        note = "Today is partial within the current 7-day window."
    else:  # this_month_vs_last_month
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
        return {"has_data": False}
    out: dict = {
        "has_data": True,
        "current": {"label": labels[0], "kwh": _r(cur_kwh)},
        "previous": {"label": labels[1], "kwh": _r(prev_kwh)},
        "difference_kwh": _r(cur_kwh - prev_kwh),
        "percent_change": _pct(cur_kwh, prev_kwh),
    }
    if out["percent_change"] is None:
        out["no_previous_data"] = True
    if note:
        out["note"] = note
    return out


def _compare_text(data: dict) -> str:
    if not data.get("has_data"):
        return "No readings were recorded in either period."
    cur = data["current"]
    prev = data["previous"]
    diff = data["difference_kwh"]
    pct = data.get("percent_change")
    direction = "more" if diff >= 0 else "less"
    abs_diff = abs(diff)
    base = (
        f"You used {cur['kwh']:.1f} kilowatt-hours {cur['label']} "
        f"versus {prev['kwh']:.1f} kilowatt-hours {prev['label']}."
    )
    if pct is not None:
        return base + f" That is {abs_diff:.1f} kilowatt-hours {direction}, a {abs(pct):.1f} percent change."
    return base


def _device_breakdown(ctx: ToolContext, a: dict) -> dict:
    t = ctx.today
    period = a["period"]
    start = {
        "today": ctx.day(t),
        "last_7_days": ctx.day(t - timedelta(days=6)),
        "this_month": ctx.day(t.replace(day=1)),
    }[period]
    per = usage.usage_by_device(ctx.db, ctx.household_id, start, ctx.day(t + timedelta(days=1)))
    names = dict(
        ctx.db.execute(
            select(Device.id, Device.name).where(
                Device.household_id == ctx.household_id, Device.deleted_at.is_(None)
            )
        ).all()
    )
    total = sum(v for k, v in per.items() if k in names)
    if total <= 0:
        return {"has_data": False, "period": period}
    rows = sorted(((names[k], v) for k, v in per.items() if k in names), key=lambda x: x[1], reverse=True)
    return {
        "has_data": True,
        "period": period,
        "total_kwh": _r(total),
        "devices": [{"name": n, "kwh": _r(v), "share_pct": round(v / total * 100, 1)} for n, v in rows],
    }


def _device_breakdown_text(data: dict) -> str:
    if not data.get("has_data"):
        period = data.get("period", "?").replace("_", " ")
        return f"No device usage was recorded for {period}."
    top = data["devices"][0] if data["devices"] else None
    total = data["total_kwh"]
    if top:
        return (
            f"Your devices used {total:.1f} kilowatt-hours in total. "
            f"The top device was {top['name']} at {top['kwh']:.1f} kilowatt-hours "
            f"({top['share_pct']:.0f} percent of total)."
        )
    return f"Your devices used {total:.1f} kilowatt-hours in total."


def _cost(ctx: ToolContext, _: dict) -> dict:
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    c = d.cost
    if c.basis == "No tariff configured":
        return {"has_data": False}
    result: dict = {
        "has_data": True,
        "is_estimate": True,
        "currency": c.currency,
        "cost_today": c.today,
        "cost_month_to_date": c.month_to_date,
        "basis": c.basis,
        "kwh_month_to_date": d.month.kwh_this_period,
    }
    if d.has_solar:
        result["solar_kwh_month_to_date"] = _r(d.solar_today_kwh)
        result["net_kwh_month_to_date"] = _r(d.net_today_kwh)
        result["note"] = "Cost is calculated on net grid consumption after solar offset."
    return result


def _cost_text(data: dict) -> str:
    if not data.get("has_data"):
        return "No tariff is configured, so the cost cannot be estimated."
    currency = data.get("currency", "")
    currency_words = {"INR": "rupees", "USD": "dollars", "GBP": "pounds", "EUR": "euros"}
    unit = currency_words.get(currency, currency.lower())
    today = data.get("cost_today", 0)
    mtd = data.get("cost_month_to_date", 0)
    base = (
        f"Today's estimated electricity cost is {today:.2f} {unit}. "
        f"Month to date it is {mtd:.2f} {unit}. This is an estimate."
    )
    if "note" in data:
        base += " Cost is calculated on net grid consumption after solar offset."
    return base


def _solar_generation(ctx: ToolContext, a: dict) -> dict:
    """Return solar generation stats for the last N days."""
    from app.services import usage as _usage
    days = a["days"]
    dates = [ctx.today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    values = _usage.daily_series(ctx.db, ctx.household_id, dates, ctx.tz, kind=_usage.GENERATOR)
    total = sum(values)
    if total <= 0:
        return {"has_data": False, "days": days, "reason": "No solar generation recorded or no solar devices registered."}
    peak = max(range(len(values)), key=values.__getitem__)
    # Also get today's current generation and net
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    return {
        "has_data": True,
        "days": days,
        "total_kwh_generated": _r(total),
        "average_kwh_per_day": _r(total / days),
        "best_day": {"date": dates[peak].isoformat(), "kwh": _r(values[peak])},
        "today_kwh_generated": _r(d.solar_today_kwh),
        "current_output_w": d.solar_current_w,
        "today_consumption_kwh": _r(d.today_kwh),
        "today_net_grid_kwh": _r(d.net_today_kwh),
        "daily": [{"date": dt.isoformat(), "kwh_generated": _r(v)} for dt, v in zip(dates, values)],
    }


def _solar_generation_text(data: dict) -> str:
    if not data.get("has_data"):
        return data.get("reason", "No solar generation data available.")
    days = data["days"]
    total = data["total_kwh_generated"]
    avg = data["average_kwh_per_day"]
    today_gen = data.get("today_kwh_generated", 0)
    net = data.get("today_net_grid_kwh", 0)
    sw = data.get("current_output_w")
    power_str = f"{_r(sw, 0):.0f} watts" if sw is not None else "not currently generating"
    return (
        f"Over the last {days} days your solar panels generated {total:.1f} kilowatt-hours in total, "
        f"averaging {avg:.1f} kilowatt-hours per day. "
        f"Today they have generated {today_gen:.1f} kilowatt-hours so far. "
        f"Current solar output is {power_str}. "
        f"Net grid draw today is {net:.1f} kilowatt-hours."
    )


def _opportunities(ctx: ToolContext, _: dict) -> dict:
    d = build_dashboard(ctx.db, ctx.household_id, 7, ctx.now)
    if not d.opportunities:
        return {
            "has_data": False,
            "checked": "overnight standby (02:00–05:00, >50 W, on ≥3 of last 7 nights)",
            "interpretation": (
                "No device was found drawing significant power overnight on most nights. "
                "This check does not cover daytime usage or overall consumption levels."
            ),
        }
    return {"has_data": True, "opportunities": [o.model_dump() for o in d.opportunities]}


def _opportunities_text(data: dict) -> str:
    if not data.get("has_data"):
        return (
            "No overnight standby waste was detected (checked for devices drawing >50 W "
            "between 02:00–05:00 on at least 3 of the last 7 nights). "
            + data.get("interpretation", "")
        )
    ops = data.get("opportunities", [])
    if not ops:
        return "No savings opportunities were found."
    first = ops[0]
    rest = len(ops) - 1
    base = first.get("title", "an opportunity") + ". " + first.get("detail", "")
    if rest > 0:
        base += f" There are {rest} more suggestion{'s' if rest > 1 else ''} in the full results."
    return base.strip()


def _tariff_info(ctx: ToolContext, _: dict) -> dict:
    """Return the household's configured electricity tariff."""
    from sqlalchemy import select as _select
    from app.models import Tariff
    tariff = ctx.db.scalar(_select(Tariff).where(Tariff.household_id == ctx.household_id))
    if tariff is None:
        return {"has_data": False, "message": "No tariff has been configured for this household."}
    result: dict = {
        "has_data": True,
        "name": tariff.name,
        "currency": tariff.currency,
        "type": tariff.type,
        "fixed_monthly_charge": tariff.fixed_monthly_charge,
        "tax_percent": tariff.tax_percent,
    }
    if tariff.type == "flat":
        result["flat_rate_per_kwh"] = tariff.flat_rate_per_kwh
    elif tariff.type == "slab":
        result["slabs"] = tariff.slabs
    elif tariff.type == "time_of_use":
        result["tou_periods"] = tariff.tou_periods
    return result


def _tariff_info_text(data: dict) -> str:
    if not data.get("has_data"):
        return "No tariff is configured for this household."
    currency = data.get("currency", "")
    name = data.get("name", "")
    kind = data.get("type", "")
    parts = [f"Tariff: {name} ({currency}, {kind})"]
    if kind == "flat":
        parts.append(f"Rate: {data.get('flat_rate_per_kwh'):.4g} {currency}/kWh")
    elif kind == "slab":
        slabs = data.get("slabs") or []
        for i, s in enumerate(slabs):
            limit = f"up to {s['up_to_kwh']} kWh" if s.get("up_to_kwh") else "above previous slab"
            parts.append(f"Slab {i+1}: {limit} at {s['rate_per_kwh']:.4g} {currency}/kWh")
    elif kind == "time_of_use":
        for p in data.get("tou_periods") or []:
            parts.append(f"{p['start']}–{p['end']}: {p['rate_per_kwh']:.4g} {currency}/kWh")
    fixed = data.get("fixed_monthly_charge", 0)
    tax = data.get("tax_percent", 0)
    if fixed:
        parts.append(f"Fixed monthly charge: {fixed:.2f} {currency}")
    if tax:
        parts.append(f"Tax: {tax:g}%")
    return ". ".join(parts) + "."


def _household_context(ctx: ToolContext, _: dict) -> dict:
    """Return a summary of household devices and data coverage."""
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select
    from app.models import Device, Reading
    from app.services import usage as _usage

    devices = ctx.db.execute(
        _select(Device.id, Device.name, Device.kind, Device.status, Device.source, Device.last_seen_at)
        .where(Device.household_id == ctx.household_id, Device.deleted_at.is_(None))
    ).all()

    if not devices:
        return {"has_data": False, "message": "No devices are registered for this household."}

    device_list = [
        {
            "name": row.name,
            "kind": row.kind,
            "status": row.status,
            "source": row.source,
            "last_seen_at": iso_z(row.last_seen_at) if row.last_seen_at else None,
        }
        for row in devices
    ]

    has_solar = any(d.kind == "generator" for d in devices)

    # Determine data coverage window
    earliest_q = ctx.db.scalar(
        _select(_func.min(Reading.measured_at))
        .select_from(Reading)
        .join(Device, Device.id == Reading.device_id)
        .where(Device.household_id == ctx.household_id, Device.deleted_at.is_(None))
    )
    latest_q = _usage.last_reading_at(ctx.db, ctx.household_id)

    return {
        "has_data": True,
        "device_count": len(device_list),
        "has_solar": has_solar,
        "devices": device_list,
        "data_from": iso_z(earliest_q) if earliest_q else None,
        "data_until": iso_z(latest_q) if latest_q else None,
    }


def _household_context_text(data: dict) -> str:
    if not data.get("has_data"):
        return "No devices are registered for this household."
    n = data["device_count"]
    solar = " including solar generation" if data.get("has_solar") else ""
    names = [d["name"] for d in data.get("devices", [])]
    device_str = ", ".join(names[:5])
    if len(names) > 5:
        device_str += f" and {len(names) - 5} more"
    frm = data.get("data_from", "unknown")
    until = data.get("data_until", "unknown")
    return (
        f"This household has {n} device{'s' if n != 1 else ''}{solar}: {device_str}. "
        f"Readings are available from {frm} to {until}."
    )


# ─── Tool registry ────────────────────────────────────────────────────────────

TOOLS: dict[str, McpTool] = {
    t.name: t
    for t in [
        McpTool(
            name="get_current_usage",
            title="Current Power Usage",
            description=(
                "Get the household's current power draw in watts and the energy "
                "consumed so far today in kilowatt-hours."
            ),
            input_schema=_empty_schema(),
            handler=_current_usage,
            label_fn=lambda _: "current power and today's usage",
        ),
        McpTool(
            name="get_daily_usage",
            title="Daily Energy Usage",
            description=(
                "Get the energy used (kWh) per day for the last N days, "
                "with the total, average, and the highest-usage day."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 90,
                        "description": "Number of days to look back (1–90).",
                    }
                },
                "required": ["days"],
                "additionalProperties": False,
            },
            handler=_daily_usage,
            label_fn=lambda a: f"last {a.get('days')} days",
        ),
        McpTool(
            name="compare_energy_usage",
            title="Compare Energy Usage",
            description=(
                "Compare energy use between two periods and return the difference "
                "and percent change. Weeks are rolling 7-day windows."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comparison": {
                        "type": "string",
                        "enum": [
                            "today_vs_yesterday",
                            "this_week_vs_last_week",
                            "this_month_vs_last_month",
                        ],
                        "description": "Which pair of periods to compare.",
                    }
                },
                "required": ["comparison"],
                "additionalProperties": False,
            },
            handler=_compare,
            label_fn=lambda a: str(a.get("comparison", "")).replace("_", " "),
        ),
        McpTool(
            name="get_device_breakdown",
            title="Device Energy Breakdown",
            description=(
                "Get the energy used per device (kWh and share of total) "
                "for a chosen period."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["today", "last_7_days", "this_month"],
                        "description": "Time period for the breakdown.",
                    }
                },
                "required": ["period"],
                "additionalProperties": False,
            },
            handler=_device_breakdown,
            label_fn=lambda a: f"devices, {str(a.get('period', '')).replace('_', ' ')}",
        ),
        McpTool(
            name="get_cost_estimate",
            title="Electricity Cost Estimate",
            description=(
                "Get the estimated electricity cost for today and the month to date, "
                "using the household's configured tariff. For households with solar, "
                "cost is calculated on net grid consumption after solar offset."
            ),
            input_schema=_empty_schema(),
            handler=_cost,
            label_fn=lambda _: "cost today and month to date",
        ),
        McpTool(
            name="get_savings_opportunities",
            title="Savings Opportunities",
            description="Get energy-saving opportunities detected from the recorded readings.",
            input_schema=_empty_schema(),
            handler=_opportunities,
            label_fn=lambda _: "savings opportunities",
        ),
        McpTool(
            name="get_solar_generation",
            title="Solar Generation",
            description=(
                "Get solar panel generation stats for the last N days: total kWh generated, "
                "daily breakdown, best day, current output, and today's net grid draw "
                "(consumption minus solar). Returns no-data if no solar devices are registered."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 90,
                        "description": "Number of days to look back (1–90).",
                    }
                },
                "required": ["days"],
                "additionalProperties": False,
            },
            handler=_solar_generation,
            label_fn=lambda a: f"solar generation last {a.get('days')} days",
        ),
        McpTool(
            name="get_tariff_info",
            title="Tariff Information",
            description=(
                "Get the household's configured electricity tariff: type (flat/slab/time_of_use), "
                "rate(s), currency, fixed monthly charge, and tax. "
                "Call this before answering any question about cost, savings, or tariff comparison."
            ),
            input_schema=_empty_schema(),
            handler=_tariff_info,
            label_fn=lambda _: "tariff information",
        ),
        McpTool(
            name="get_household_context",
            title="Household Context",
            description=(
                "Get the list of registered devices (name, kind, status, source) and the date "
                "range of available readings. Call this to understand what appliances are tracked "
                "and how much historical data is available before making recommendations."
            ),
            input_schema=_empty_schema(),
            handler=_household_context,
            label_fn=lambda _: "household devices and data coverage",
        ),
    ]
}

# Text renderers keyed by tool name (used to build the spoken 'text' field)
_TEXT_RENDERERS: dict[str, Callable[[dict], str]] = {
    "get_current_usage": _current_usage_text,
    "get_daily_usage": _daily_usage_text,
    "compare_energy_usage": _compare_text,
    "get_device_breakdown": _device_breakdown_text,
    "get_cost_estimate": _cost_text,
    "get_savings_opportunities": _opportunities_text,
    "get_solar_generation": _solar_generation_text,
    "get_tariff_info": _tariff_info_text,
    "get_household_context": _household_context_text,
}


def render_text(name: str, data: dict) -> str:
    fn = _TEXT_RENDERERS.get(name)
    if fn is None:
        return "Result unavailable."
    return fn(data)
