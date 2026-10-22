import secrets

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, membership_or_404, require_admin
from app.errors import AppError
from app.models import Device, User
from app.schemas import (
    DeviceCreatedOut,
    DeviceCreateIn,
    DeviceListOut,
    DeviceOut,
    DevicePatchIn,
    SecretOut,
)
from app.security import sha256_hex
from app.utils import utcnow

router = APIRouter(prefix="/devices", tags=["devices"])


def _new_secret() -> str:
    return "hp_" + secrets.token_urlsafe(32)


def _device_or_404(db: Session, user: User, device_id: int, admin: bool) -> Device:
    device = db.scalar(select(Device).where(Device.id == device_id, Device.deleted_at.is_(None)))
    if device is None:
        raise AppError(404, "not_found", "Not found")
    membership = membership_or_404(db, user, device.household_id)
    if admin:
        require_admin(membership)
    return device


@router.get("", response_model=DeviceListOut)
def list_devices(
    household_id: int = Query(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership_or_404(db, user, household_id)
    items = db.scalars(
        select(Device)
        .where(Device.household_id == household_id, Device.deleted_at.is_(None))
        .order_by(Device.id)
    ).all()
    return DeviceListOut(items=[DeviceOut.model_validate(d) for d in items], next_cursor=None)


@router.post("", status_code=201, response_model=DeviceCreatedOut)
def create_device(body: DeviceCreateIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    require_admin(membership_or_404(db, user, body.household_id))
    secret = _new_secret()
    device = Device(
        household_id=body.household_id,
        name=body.name,
        source=body.source,
        status="active",
        secret_hash=sha256_hex(secret),
    )
    db.add(device)
    db.commit()
    return DeviceCreatedOut(**DeviceOut.model_validate(device).model_dump(), ingestion_secret=secret)


@router.patch("/{device_id}", response_model=DeviceOut)
def patch_device(
    device_id: int, body: DevicePatchIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    device = _device_or_404(db, user, device_id, admin=True)
    if body.name is not None:
        device.name = body.name
    if body.status is not None:
        device.status = body.status
    db.commit()
    return device


@router.post("/{device_id}/rotate-secret", response_model=SecretOut)
def rotate_secret(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    device = _device_or_404(db, user, device_id, admin=True)
    secret = _new_secret()
    device.secret_hash = sha256_hex(secret)
    db.commit()
    return SecretOut(ingestion_secret=secret)


@router.delete("/{device_id}", status_code=204, response_class=Response)
def delete_device(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    device = _device_or_404(db, user, device_id, admin=True)
    device.deleted_at = utcnow()
    device.secret_hash = ""  # no secret can match an empty hash
    db.commit()
    return Response(status_code=204)
