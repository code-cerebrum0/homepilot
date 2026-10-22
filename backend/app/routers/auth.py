from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.cookies import REFRESH, clear_session_cookies, set_session_cookies
from app.db import get_db
from app.deps import get_current_user
from app.errors import AppError, error_response
from app.models import Household, Membership, RefreshSession, User
from app.rate_limit import login_limiter
from app.schemas import LoginIn, MeOut, RegisterIn
from app.security import (
    DUMMY_HASH,
    create_access_token,
    hash_password,
    new_opaque_token,
    sha256_hex,
    verify_password,
)
from app.services.accounts import build_me
from app.utils import utcnow

router = APIRouter(prefix="/auth", tags=["auth"])


def _start_session(db: Session, user: User, response: Response, family_id: str | None = None) -> None:
    raw = new_opaque_token()
    db.add(
        RefreshSession(
            user_id=user.id,
            family_id=family_id or str(uuid4()),
            token_hash=sha256_hex(raw),
            expires_at=utcnow() + timedelta(days=get_settings().refresh_ttl_days),
        )
    )
    set_session_cookies(response, create_access_token(user.id), raw, new_opaque_token())


def _revoke_family(db: Session, family_id: str) -> None:
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.family_id == family_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


@router.post("/register", status_code=201, response_model=MeOut)
def register(body: RegisterIn, response: Response, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise AppError(409, "conflict", "An account with this email already exists")
    user = User(name=body.name, email=email, password_hash=hash_password(body.password))
    try:
        db.add(user)
        db.flush()
        household = Household(name=f"{body.name}'s Home")
        db.add(household)
        db.flush()
        db.add(Membership(user_id=user.id, household_id=household.id, role="owner"))
        _start_session(db, user, response)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "conflict", "An account with this email already exists")
    return build_me(db, user)


@router.post("/login", response_model=MeOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    email = body.email.lower()
    ip = request.client.host if request.client else "unknown"
    key = f"{ip}:{email}"
    if login_limiter.count(key, s.login_window_seconds) >= s.login_max_failures:
        raise AppError(429, "rate_limited", "Too many failed attempts. Please try again later.")

    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash if user else DUMMY_HASH, body.password)  # constant-ish time
    if user is None or not ok:
        login_limiter.add(key)
        raise AppError(401, "invalid_credentials", "Invalid email or password")

    login_limiter.clear(key)
    _start_session(db, user, response)
    db.commit()
    return build_me(db, user)


@router.post("/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    def fail():
        r = error_response(401, "unauthorized", "Session expired")
        clear_session_cookies(r)
        return r

    raw = request.cookies.get(REFRESH)
    if not raw:
        return fail()
    session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == sha256_hex(raw)))
    if session is None:
        return fail()

    now = utcnow()
    if session.revoked_at is not None:
        # An already-rotated/revoked token was presented: treat as theft, kill the whole family.
        _revoke_family(db, session.family_id)
        db.commit()
        return fail()
    if session.expires_at <= now:
        return fail()
    user = db.get(User, session.user_id)
    if user is None:
        return fail()

    session.revoked_at = now
    _start_session(db, user, response, family_id=session.family_id)
    db.commit()
    return {}


@router.post("/logout", status_code=204, response_class=Response)
def logout(request: Request, db: Session = Depends(get_db)):
    raw = request.cookies.get(REFRESH)
    if raw:
        session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == sha256_hex(raw)))
        if session:
            _revoke_family(db, session.family_id)
            db.commit()
    resp = Response(status_code=204)
    clear_session_cookies(resp)
    return resp


@router.post("/logout-all", status_code=204, response_class=Response)
def logout_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.commit()
    resp = Response(status_code=204)
    clear_session_cookies(resp)
    return resp
