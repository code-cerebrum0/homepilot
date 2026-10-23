"""Savings opportunity detection for HomePilot.

Analyses stored, non-simulated readings to surface actionable energy-saving
recommendations grounded in actual household data.

Status values returned by find_opportunities:
  "ok"               – analysis ran; opportunities list may be empty.
  "insufficient_data"– not enough readings to support reliable analysis.
  "error"            – internal failure.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Device, Reading
from app.schemas import OpportunityOut
from app.utils import local_midnight

# ── Configuration ──────────────────────────────────────────────────────────────

NIGHT_START_H, NIGHT_END_H = 2, 5          # 02:00–05:00 local
NIGHTS = 7                                  # look back over this many nights
STANDBY_THRESHOLD_W = 50.0                  # W — meaningful overnight standby
MIN_SAMPLES_PER_NIGHT = 6                   # readings required to trust a night's average
MIN_NIGHTS_ON = 3                           # nights that must be "on" before flagging
MIN_NIGHTS_WITH_DATA = 3                    # nights with data needed to call the check meaningful
HIGH_DAILY_THRESHOLD_KWH = 20.0            # kWh/day per device – flag if consistently above this
MIN_DAYS_FOR_HIGH_USAGE = 5                 # days of data needed before flagging high usage


def _night_window(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    base = local_midnight(d, tz)
    return base + timedelta(hours=NIGHT_START_H), base + timedelta(hours=NIGHT_END_H)


@dataclass
class OpportunityResult:
    """Result of the savings-opportunity analysis.

    status values:
      "ok"               – ran successfully; opportunities list valid (may be empty).
      "insufficient_data"– not enough readings to support analysis.
      "error"            – internal failure.
    """
    status: str                                # "ok" | "insufficient_data" | "error"
    opportunities: list[OpportunityOut] = field(default_factory=list)
    checked_rules: list[str] = field(default_factory=list)  # human-readable rule descriptions
    nights_with_data: int = 0
    reading_days: int = 0
    message: str = ""                          # additional context for the LLM


def find_opportunities(
    db: Session,
    household_id: int,
    now: datetime,
    tz: ZoneInfo,
) -> OpportunityResult:
    """Detect energy-saving opportunities from stored readings.

    Filters to consumer-kind devices only (solar generators are excluded).
    Only uses non-simulated readings.

    Returns an OpportunityResult with an explicit status.
    """

    # ── 1. Fetch active consumer devices ──────────────────────────────────────
    devices = db.execute(
        select(Device.id, Device.name).where(
            Device.household_id == household_id,
            Device.deleted_at.is_(None),
            Device.status == "active",
            Device.source != "simulated",
            Device.kind == "consumer",   # generators (solar) are explicitly excluded
        )
    ).all()

    if not devices:
        return OpportunityResult(
            status="insufficient_data",
            message=(
                "No active, non-simulated consumer devices are registered for this household. "
                "Savings analysis requires at least one monitored appliance."
            ),
        )

    names = {row.id: row.name for row in devices}
    device_ids = list(names.keys())
    today = now.astimezone(tz).date()

    # ── 2. Build the list of complete night windows to check ──────────────────
    nights: list[tuple[datetime, datetime]] = []
    for k in range(NIGHTS + 1):
        s, e = _night_window(today - timedelta(days=k), tz)
        if e <= now:
            nights.append((s, e))
        if len(nights) == NIGHTS:
            break

    # ── 3. Overnight-standby check ────────────────────────────────────────────
    #   Per device: count nights with sufficient data, and nights where average W ≥ threshold.
    stats: dict[int, dict] = {i: {"with_data": 0, "on": 0, "watts": []} for i in device_ids}
    for s, e in nights:
        rows = db.execute(
            select(Reading.device_id, func.avg(Reading.power_w), func.count(Reading.id))
            .where(
                Reading.device_id.in_(device_ids),
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

    total_nights_with_data = max((st["with_data"] for st in stats.values()), default=0)

    # ── 4. Determine data sufficiency ─────────────────────────────────────────
    checked_rules = [
        f"overnight standby ({NIGHT_START_H:02d}:00–{NIGHT_END_H:02d}:00, "
        f">={STANDBY_THRESHOLD_W:.0f} W, "
        f"on ≥{MIN_NIGHTS_ON} of last {NIGHTS} nights with ≥{MIN_SAMPLES_PER_NIGHT} readings/night)"
    ]

    if total_nights_with_data < MIN_NIGHTS_WITH_DATA:
        return OpportunityResult(
            status="insufficient_data",
            checked_rules=checked_rules,
            nights_with_data=total_nights_with_data,
            message=(
                f"Only {total_nights_with_data} of the last {NIGHTS} nights had sufficient "
                f"readings (≥{MIN_SAMPLES_PER_NIGHT}/night) to check for overnight standby waste. "
                f"At least {MIN_NIGHTS_WITH_DATA} nights are needed for a reliable result."
            ),
        )

    # ── 5. Emit overnight-standby opportunities ────────────────────────────────
    opportunities: list[OpportunityOut] = []
    hours = NIGHT_END_H - NIGHT_START_H

    for dev_id, st in stats.items():
        if st["with_data"] < MIN_NIGHTS_WITH_DATA:
            continue
        if st["on"] < MIN_NIGHTS_ON:
            continue
        # Require at least 50 % of checked nights to be "on" (avoids single-outlier flags)
        if st["on"] / st["with_data"] < 0.5:
            continue

        avg_w = sum(st["watts"]) / len(st["watts"])
        # Monthly saving estimate: average W × 3 h/night × 30 nights × fraction nights on
        fraction = st["on"] / st["with_data"]
        saving_kwh = avg_w / 1000 * hours * 30 * fraction
        opportunities.append(
            OpportunityOut(
                title=f"{names[dev_id]} runs overnight",
                detail=(
                    f"{names[dev_id]} averaged {avg_w:.0f} W between "
                    f"{NIGHT_START_H:02d}:00–{NIGHT_END_H:02d}:00 on "
                    f"{st['on']} of {st['with_data']} checked nights "
                    f"({fraction * 100:.0f}% of nights with data). "
                    f"Estimated saving if turned off at night: "
                    f"{saving_kwh:.1f} kWh/month "
                    f"(assumption: {avg_w:.0f} W × {hours} h × 30 nights × {fraction:.0%} occupancy rate)."
                ),
                estimated_monthly_saving_kwh=round(saving_kwh, 1),
            )
        )

    opportunities.sort(key=lambda o: o.estimated_monthly_saving_kwh or 0, reverse=True)

    if not opportunities:
        # Analysis ran successfully but no rule triggered — be explicit about what was checked
        return OpportunityResult(
            status="ok",
            opportunities=[],
            checked_rules=checked_rules,
            nights_with_data=total_nights_with_data,
            message=(
                f"Analysis checked {total_nights_with_data} nights of data. "
                "No device was drawing significant power overnight on most nights. "
                "This check covers overnight standby only — it does not analyse daytime "
                "consumption levels, tariff optimisation, or solar-shifting opportunities."
            ),
        )

    return OpportunityResult(
        status="ok",
        opportunities=opportunities[:3],
        checked_rules=checked_rules,
        nights_with_data=total_nights_with_data,
        message=(
            f"Analysis checked {total_nights_with_data} nights of data and found "
            f"{len(opportunities)} opportunity{'s' if len(opportunities) != 1 else ''}."
        ),
    )
