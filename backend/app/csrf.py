import hmac

from starlette.middleware.base import BaseHTTPMiddleware

from app.cookies import ACCESS, CSRF, REFRESH
from app.errors import error_response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Public entry points and device ingestion (authenticated by device secret, not cookies).
EXEMPT_PATHS = {"/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/readings"}

# Path prefixes exempt from CSRF (use bearer tokens or signed form token, not cookies).
EXEMPT_PREFIXES = ("/mcp/", "/oauth/", "/.well-known/")


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit check: X-CSRF-Token header must equal the csrf_token cookie.

    Applied to unsafe methods whenever the request carries a session cookie."""

    async def dispatch(self, request, call_next):
        path = request.url.path.rstrip("/")
        # Exempt bearer-token protected paths from CSRF entirely
        if any(request.url.path.startswith(pfx) for pfx in EXEMPT_PREFIXES):
            return await call_next(request)
        if request.method not in SAFE_METHODS and path not in EXEMPT_PATHS:
            if request.cookies.get(ACCESS) or request.cookies.get(REFRESH):
                cookie = request.cookies.get(CSRF)
                header = request.headers.get("x-csrf-token")
                if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
                    return error_response(403, "forbidden", "CSRF token missing or invalid")
        return await call_next(request)
