from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cookies import ACCESS
from app.db import get_db
from app.errors import AppError
from app.models import Membership, User
from app.security import decode_access_token

ADMIN_ROLES = {"owner", "admin"}


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(ACCESS)
    user_id = decode_access_token(token) if token else None
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise AppError(401, "unauthorized", "Not authenticated")
    return user


def membership_or_404(db: Session, user: User, household_id: int) -> Membership:
    """404 (not 403) when the caller isn't a member, so IDs can't be probed."""
    m = db.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.household_id == household_id)
    )
    if m is None:
        raise AppError(404, "not_found", "Not found")
    return m


def require_admin(m: Membership) -> None:
    if m.role not in ADMIN_ROLES:
        raise AppError(403, "forbidden", "You do not have permission to do this")
