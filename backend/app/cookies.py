from fastapi import Response

from app.config import get_settings

ACCESS = "access_token"
REFRESH = "refresh_token"
CSRF = "csrf_token"
REFRESH_PATH = "/api/v1/auth"


def set_session_cookies(resp: Response, access: str, refresh: str, csrf: str) -> None:
    s = get_settings()
    refresh_age = s.refresh_ttl_days * 86400
    common = {"secure": s.cookie_secure, "samesite": "lax"}
    resp.set_cookie(ACCESS, access, max_age=s.access_ttl_minutes * 60, httponly=True, path="/", **common)
    resp.set_cookie(REFRESH, refresh, max_age=refresh_age, httponly=True, path=REFRESH_PATH, **common)
    resp.set_cookie(CSRF, csrf, max_age=refresh_age, httponly=False, path="/", **common)


def clear_session_cookies(resp: Response) -> None:
    s = get_settings()
    common = {"secure": s.cookie_secure, "samesite": "lax"}
    resp.delete_cookie(ACCESS, httponly=True, path="/", **common)
    resp.delete_cookie(REFRESH, httponly=True, path=REFRESH_PATH, **common)
    resp.delete_cookie(CSRF, httponly=False, path="/", **common)
