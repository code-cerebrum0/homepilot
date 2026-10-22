from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import MeOut, UserPatchIn
from app.services.accounts import build_me

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return build_me(db, user)


@router.patch("/me", response_model=MeOut)
def patch_me(body: UserPatchIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.name = body.name
    db.add(user)
    db.commit()
    return build_me(db, user)
