import base64
import hmac
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, membership_or_404
from app.errors import AppError
from app.models import Device, Reading, User
from app.rate_limit import device_limiter
from app.schemas import ReadingIn, ReadingListOut, ReadingOut
from app.security import sha256_hex
from app.utils import utcnow

router = APIRouter(prefix="/readings", tags=["readings"])

MAX_FUTURE_SKEW = timedelta(minutes=5)


def _encode_cursor(measured_at: datetime, reading_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([measured_at.isoformat(), reading_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        m, i = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(m), int(i)
    except Exception:
        raise AppError(422, "validation_error", "Invalid cursor")


@router.get("", response_model=ReadingListOut)
def list_readings(
    device_id: int = Query(...),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    device = db.scalar(select(Device).where(Device.id == device_id, Device.deleted_at.is_(None)))
    if device is None:
        raise AppError(404, "not_found", "Not found")
    membership_or_404(db, user, device.household_id)

    q = select(Reading).where(Reading.device_id == device_id)
    if cursor:
        m, i = _decode_cursor(cursor)
        q = q.where(or_(Reading.measured_at < m, and_(Reading.measured_at == m, Reading.id < i)))
    rows = db.scalars(q.order_by(Reading.measured_at.desc(), Reading.id.desc()).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1].measured_at, rows[-1].id) if has_more and rows else None
    return ReadingListOut(items=[ReadingOut.model_validate(r) for r in rows], next_cursor=next_cursor)


def _authenticate_device(db: Session, body: ReadingIn, authorization: str | None, x_device_id: str | None) -> Device:
    unauthorized = AppError(401, "unauthorized", "Invalid device credentials")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized
    secret = authorization[7:].strip()
    if x_device_id is not None and x_device_id != str(body.device_id):
        raise unauthorized
    device = db.scalar(select(Device).where(Device.id == body.device_id, Device.deleted_at.is_(None)))
    if device is None or not device.secret_hash:
        raise unauthorized
    if not hmac.compare_digest(device.secret_hash, sha256_hex(secret)):
        raise unauthorized
    return device


@router.post("", response_model=ReadingOut, status_code=201)
def ingest_reading(
    body: ReadingIn,
    response: Response,
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Device-authenticated ingestion (not called by the browser frontend)."""
    device = _authenticate_device(db, body, authorization, x_device_id)
    if device.status != "active":
        raise AppError(403, "forbidden", "Device is disabled")
    if not device_limiter.allow(f"device:{device.id}", get_settings().device_rate_limit_per_minute):
        raise AppError(429, "rate_limited", "Too many readings from this device")

    if body.idempotency_key:
        existing = db.scalar(
            select(Reading).where(Reading.device_id == device.id, Reading.idempotency_key == body.idempotency_key)
        )
        if existing:
            response.status_code = 200
            return existing

    now = utcnow()
    if body.measured_at > now + MAX_FUTURE_SKEW:
        raise AppError(422, "validation_error", "measured_at is in the future")
    expected = body.power_w * body.interval_seconds / 3.6e6
    if abs(body.energy_kwh - expected) > max(0.0005, expected * 0.25):
        raise AppError(422, "validation_error", "energy_kwh is inconsistent with power_w and interval_seconds")

    reading = Reading(
        device_id=device.id,
        measured_at=body.measured_at,
        ingested_at=now,
        power_w=body.power_w,
        energy_kwh=body.energy_kwh,
        interval_seconds=body.interval_seconds,
        is_simulated=device.source == "simulated",
        idempotency_key=body.idempotency_key,
    )
    db.add(reading)
    device.last_seen_at = now
    try:
        db.commit()
    except IntegrityError:  # concurrent duplicate idempotency_key
        db.rollback()
        existing = db.scalar(
            select(Reading).where(Reading.device_id == device.id, Reading.idempotency_key == body.idempotency_key)
        )
        if existing is None:
            raise
        response.status_code = 200
        return existing
    return reading
