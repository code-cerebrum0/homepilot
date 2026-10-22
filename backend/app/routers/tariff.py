from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, membership_or_404, require_admin
from app.errors import AppError
from app.models import Tariff, User
from app.schemas import TariffIn, TariffOut

router = APIRouter(tags=["tariff"])


@router.get("/households/{household_id}/tariff", response_model=TariffOut)
def get_tariff(household_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    membership_or_404(db, user, household_id)
    tariff = db.scalar(select(Tariff).where(Tariff.household_id == household_id))
    if tariff is None:
        raise AppError(404, "not_found", "No tariff configured")
    return tariff


@router.put("/households/{household_id}/tariff", response_model=TariffOut)
def put_tariff(
    household_id: int, body: TariffIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    require_admin(membership_or_404(db, user, household_id))
    tariff = db.scalar(select(Tariff).where(Tariff.household_id == household_id))
    if tariff is None:
        tariff = Tariff(household_id=household_id)
        db.add(tariff)
    tariff.name = body.name
    tariff.currency = body.currency
    tariff.type = body.type
    tariff.flat_rate_per_kwh = body.flat_rate_per_kwh
    tariff.fixed_monthly_charge = body.fixed_monthly_charge
    tariff.tax_percent = body.tax_percent
    tariff.slabs = [s.model_dump() for s in body.slabs] if body.slabs else None
    tariff.tou_periods = [p.model_dump() for p in body.tou_periods] if body.tou_periods else None
    db.commit()
    return tariff
