from datetime import date, datetime, time, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@lru_cache
def app_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def local_midnight(d: date, tz: ZoneInfo) -> datetime:
    """Start of local calendar day `d`, as an aware UTC datetime."""
    return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
