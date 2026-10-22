from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, membership_or_404
from app.errors import AppError
from app.models import User
from app.schemas import DashboardOut
from app.services.dashboard import build_dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/households/{household_id}/dashboard", response_model=DashboardOut)
def dashboard(
    household_id: int,
    days: int = Query(7),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership_or_404(db, user, household_id)
    if days not in (7, 30, 90):
        raise AppError(422, "validation_error", "days must be one of 7, 30, 90")
    return build_dashboard(db, household_id, days)
