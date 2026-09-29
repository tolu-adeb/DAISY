"""Access control for the dashboard and REST API.

    ABG_ADMIN_TOKEN   set -> every change (POST / PUT / DELETE) needs the admin token
    ABG_VIEW_TOKEN    set -> even viewing needs a token (the view token or the admin token)

Tokens are sent as a ``Authorization: Bearer <token>`` header or an ``abg_token`` HttpOnly cookie
(set by POST /api/auth/login, so the live SSE stream works in the browser).  Comparisons are
constant-time.  With neither token set the API is open, which is only safe on 127.0.0.1 / Tailscale.
"""
from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse

OPEN_PATHS = ("/api/health", "/api/auth/", "/static/", "/favicon")
WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def _token(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return request.cookies.get("abg_token") or request.headers.get("x-abg-token")


def _eq(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def role(settings, request: Request) -> str:
    """admin | view | none"""
    if not settings.admin_token and not settings.view_token:
        return "admin"
    t = _token(request)
    if _eq(t, settings.admin_token):
        return "admin"
    if _eq(t, settings.view_token):
        return "view"
    return "none" if settings.view_token else "view"      # only an admin token set: anyone may look


def middleware(get_settings):
    async def mw(request: Request, call_next):
        s = get_settings(request)
        path = request.url.path
        if path.startswith(OPEN_PATHS) or (not s.admin_token and not s.view_token):
            return await call_next(request)
        r = role(s, request)
        if r == "none" and path != "/":
            return JSONResponse({"error": {"code": "unauthorized", "message": "sign in with your view or admin token"}},
                                status_code=401)
        if request.method in WRITE and r != "admin":
            return JSONResponse({"error": {"code": "forbidden", "message": "read-only access: changes need the admin token"}},
                                status_code=403)
        return await call_next(request)
    return mw
