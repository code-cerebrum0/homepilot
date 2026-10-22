from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Household, Membership, User
from app.schemas import MembershipOut, MeOut


def build_me(db: Session, user: User) -> MeOut:
    rows = db.execute(
        select(Membership.household_id, Household.name, Membership.role)
        .join(Household, Household.id == Membership.household_id)
        .where(Membership.user_id == user.id)
        .order_by(Membership.id)
    ).all()
    return MeOut(
        id=user.id,
        name=user.name,
        email=user.email,
        memberships=[MembershipOut(household_id=h, household_name=n, role=r) for h, n, r in rows],
    )
