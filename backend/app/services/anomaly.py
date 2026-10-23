"""Usage anomaly detection service for HomePilot.

Uses the median + MAD (Median Absolute Deviation) statistical baseline to
identify days where household or device consumption deviates significantly
from the historical norm.

Design constraints:
- Never labels the highest-consumption day as abnormal merely because it is the highest.
- Requires a sufficient number of comparable observations before asserting an anomaly.
- Returns insufficient_data when a reliable conclusion cannot be made.
- Does not claim to detect instantaneous spikes unless sampling frequency supports it.
- All queries are scoped to the authorised household.
- Read-only.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Device, Reading
from app.services.usage import CONSUMER, daily_series
from app.utils import local_midnight

# ── Configuration ──────────────────────────────────────────────────────────────

# Minimum number of historical days needed to compute a reliable baseline
MIN_BASELINE_DAYS = 7

# How many MADs above/below median before something is flagged as anomalous
MAD_THRESHOLD = 3.0

# If MAD is below this floor, use an absolute threshold instead to avoid
# false positives on very stable (near-zero) usage profiles
MIN_MAD_FLOOR_KWH = 0.5

# A day is "low data quality" if it has fewer than this many readings
MIN_READINGS_PER_DAY = 4


@dataclass
class AnomalyFinding:
    """One detected anomaly or notable observation."""
    date: str                               # ISO date, or datetime for sub-day
    device_name: str | None                 # None = household aggregate
    observed_kwh: float
    baseline_median_kwh: float
    deviation_mad: float                    # deviation expressed in MAD units
    direction: Literal["high", "low"]
    confirmed: bool                         # True = passes threshold; False = worth investigating
    explanation: str


@dataclass
class AnomalyResult:
    """Result of the anomaly analysis."""
    status: Literal["ok", "insufficient_data", "error"]
    findings: list[AnomalyFinding] = field(default_factory=list)
    analysis_period_days: int = 0
    baseline_days_used: int = 0
    baseline_median_kwh: float = 0.0
    baseline_mad_kwh: float = 0.0
    reading_coverage_pct: float = 0.0     # fraction of recent days with adequate readings
    message: str = ""


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mad(values: list[float], med: float) -> float:
    """Median Absolute Deviation."""
    if not values:
        return 0.0
    return _median([abs(v - med) for v in values])


def _reading_count_per_day(
    db: Session,
    household_id: int,
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
) -> dict[date, int]:
    """Return the number of readings per local calendar day."""
    rows = db.execute(
        select(Reading.measured_at)
        .select_from(Reading)
        .join(Device, Device.id == Reading.device_id)
        .where(
            Device.household_id == household_id,
            Device.deleted_at.is_(None),
            Device.kind == CONSUMER,
            Reading.is_simulated.is_(False),
            Reading.measured_at >= start,
            Reading.measured_at < end,
        )
    ).scalars().all()

    counts: dict[date, int] = {}
    for ts in rows:
        d = ts.astimezone(tz).date()
        counts[d] = counts.get(d, 0) + 1
    return counts


def find_anomalies(
    db: Session,
    household_id: int,
    now: datetime,
    tz: ZoneInfo,
    analysis_days: int = 7,
    history_days: int = 30,
) -> AnomalyResult:
    """Detect anomalous daily household consumption.

    Parameters
    ----------
    analysis_days:
        Number of recent days to check for anomalies (default 7).
    history_days:
        Total historical window to load for baseline calculation (default 30).
        The baseline is built from the days *before* the analysis window.
    """
    if analysis_days < 1:
        return AnomalyResult(status="error", message="analysis_days must be ≥ 1.")
    if history_days < analysis_days + MIN_BASELINE_DAYS:
        return AnomalyResult(
            status="error",
            message=(
                f"history_days ({history_days}) must be at least "
                f"analysis_days ({analysis_days}) + {MIN_BASELINE_DAYS} "
                f"= {analysis_days + MIN_BASELINE_DAYS}."
            ),
        )

    today = now.astimezone(tz).date()

    # analysis window: [today - analysis_days + 1 .. today]  (excludes today as it may be partial)
    # baseline window: [today - history_days .. today - analysis_days]
    analysis_end = today                                          # inclusive
    analysis_start = today - timedelta(days=analysis_days - 1)
    baseline_end = analysis_start - timedelta(days=1)             # day before analysis window
    baseline_start = today - timedelta(days=history_days - 1)

    all_dates = [today - timedelta(days=i) for i in range(history_days - 1, -1, -1)]
    baseline_dates = [d for d in all_dates if baseline_start <= d <= baseline_end]
    analysis_dates = [d for d in all_dates if analysis_start <= d <= analysis_end]

    # Don't include today if it's partial (less than 6 hours old or < 4 readings)
    # We simply exclude today from analysis to avoid partial-day false positives
    analysis_dates = [d for d in analysis_dates if d < today]
    if not analysis_dates:
        return AnomalyResult(
            status="insufficient_data",
            analysis_period_days=analysis_days,
            message="No complete analysis days available (today is always excluded as it may be partial).",
        )

    # ── Load daily kWh series ─────────────────────────────────────────────────
    window_start_dt = local_midnight(baseline_start, tz)
    window_end_dt = local_midnight(today + timedelta(days=1), tz)
    kwh_series = daily_series(db, household_id, all_dates, tz, kind=CONSUMER)

    date_to_kwh = {d: kwh_series[i] for i, d in enumerate(all_dates)}

    baseline_kwh = [date_to_kwh[d] for d in baseline_dates]
    analysis_kwh = [date_to_kwh[d] for d in analysis_dates]

    # ── Check reading coverage ────────────────────────────────────────────────
    counts = _reading_count_per_day(db, household_id, window_start_dt, window_end_dt, tz)
    analysis_days_with_data = sum(
        1 for d in analysis_dates if counts.get(d, 0) >= MIN_READINGS_PER_DAY
    )
    coverage_pct = analysis_days_with_data / len(analysis_dates) if analysis_dates else 0.0

    # Require enough baseline days with non-zero values
    baseline_nonzero = [v for v in baseline_kwh if v > 0]
    if len(baseline_nonzero) < MIN_BASELINE_DAYS:
        return AnomalyResult(
            status="insufficient_data",
            analysis_period_days=analysis_days,
            baseline_days_used=len(baseline_nonzero),
            message=(
                f"Only {len(baseline_nonzero)} baseline days had non-zero consumption "
                f"(need ≥{MIN_BASELINE_DAYS}). Cannot compute a reliable historical baseline."
            ),
        )

    # ── Compute baseline statistics ───────────────────────────────────────────
    med = _median(baseline_nonzero)
    mad = _mad(baseline_nonzero, med)
    effective_mad = max(mad, MIN_MAD_FLOOR_KWH)

    # ── Detect anomalies in analysis window ───────────────────────────────────
    findings: list[AnomalyFinding] = []
    for d, kwh in zip(analysis_dates, analysis_kwh):
        day_count = counts.get(d, 0)
        if day_count < MIN_READINGS_PER_DAY:
            # Not enough readings on this day — skip rather than flag
            continue
        if kwh <= 0:
            # No consumption recorded — could be a gap; don't flag as anomaly
            continue

        deviation = (kwh - med) / effective_mad
        is_high = deviation > 0
        abs_dev = abs(deviation)
        confirmed = abs_dev >= MAD_THRESHOLD

        if abs_dev >= MAD_THRESHOLD * 0.6:  # report if ≥ 60% of threshold (worth investigating)
            pct_diff = ((kwh - med) / med * 100) if med > 0 else 0.0
            direction_word = "higher" if is_high else "lower"
            if confirmed:
                explanation = (
                    f"{d.isoformat()}: {kwh:.1f} kWh consumed, which is "
                    f"{abs(pct_diff):.0f}% {direction_word} than the historical median "
                    f"of {med:.1f} kWh/day (deviation: {abs_dev:.1f} MAD; "
                    f"threshold: {MAD_THRESHOLD} MAD). "
                    f"Baseline: {len(baseline_nonzero)} days, median {med:.1f} kWh, "
                    f"MAD {mad:.2f} kWh."
                )
            else:
                explanation = (
                    f"{d.isoformat()}: {kwh:.1f} kWh consumed, which is "
                    f"{abs(pct_diff):.0f}% {direction_word} than the historical median "
                    f"of {med:.1f} kWh/day (deviation: {abs_dev:.1f} MAD; "
                    f"below the {MAD_THRESHOLD}-MAD confirmation threshold — "
                    f"worth monitoring but not yet confirmed as anomalous)."
                )
            findings.append(AnomalyFinding(
                date=d.isoformat(),
                device_name=None,
                observed_kwh=round(kwh, 2),
                baseline_median_kwh=round(med, 2),
                deviation_mad=round(abs_dev, 2),
                direction="high" if is_high else "low",
                confirmed=confirmed,
                explanation=explanation,
            ))

    # Sort confirmed anomalies first, then by deviation magnitude
    findings.sort(key=lambda f: (not f.confirmed, -f.deviation_mad))

    if not findings:
        all_reasonable = all(
            abs((date_to_kwh[d] - med) / effective_mad) < MAD_THRESHOLD * 0.6
            for d in analysis_dates
            if counts.get(d, 0) >= MIN_READINGS_PER_DAY and date_to_kwh[d] > 0
        )
        if all_reasonable:
            msg = (
                f"No anomalies detected in the last {len(analysis_dates)} complete day(s). "
                f"All days were within normal variation of the historical median "
                f"({med:.1f} kWh/day, MAD {mad:.2f} kWh, "
                f"based on {len(baseline_nonzero)} baseline days)."
            )
        else:
            msg = (
                f"No anomalies detected. "
                f"Baseline: {len(baseline_nonzero)} days, median {med:.1f} kWh/day."
            )
        return AnomalyResult(
            status="ok",
            findings=[],
            analysis_period_days=len(analysis_dates),
            baseline_days_used=len(baseline_nonzero),
            baseline_median_kwh=round(med, 2),
            baseline_mad_kwh=round(mad, 2),
            reading_coverage_pct=round(coverage_pct, 2),
            message=msg,
        )

    return AnomalyResult(
        status="ok",
        findings=findings,
        analysis_period_days=len(analysis_dates),
        baseline_days_used=len(baseline_nonzero),
        baseline_median_kwh=round(med, 2),
        baseline_mad_kwh=round(mad, 2),
        reading_coverage_pct=round(coverage_pct, 2),
        message=(
            f"Found {sum(1 for f in findings if f.confirmed)} confirmed "
            f"and {sum(1 for f in findings if not f.confirmed)} borderline anomal{'ies' if len(findings) != 1 else 'y'} "
            f"in the last {len(analysis_dates)} complete day(s). "
            f"Baseline: {len(baseline_nonzero)} days, median {med:.1f} kWh/day, MAD {mad:.2f} kWh."
        ),
    )
