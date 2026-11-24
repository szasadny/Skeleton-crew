import secrets

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from skeleton_crew.core.security import check_csrf_token, make_csrf_token
from skeleton_crew.web.state import get_state

SESSION_MAX_AGE_S = 12 * 60 * 60
LOGIN_PATH = "/login"
PUBLIC_PATHS = frozenset({LOGIN_PATH})
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
_SID = "sid"
_SID_BYTES = 32
_AUTH = "auth"
_SAFE_METHODS = frozenset({"GET", "HEAD"})

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # Without it the browser back button shows cached pages after logout.
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LoginRequired(Exception):
    pass


class CsrfFailed(Exception):
    pass


def _new_sid() -> str:
    return secrets.token_urlsafe(_SID_BYTES)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_id(request: Request) -> str:
    sid = request.session.get(_SID)
    if not isinstance(sid, str):
        sid = _new_sid()
        request.session[_SID] = sid
    return sid


def is_authenticated(request: Request) -> bool:
    return request.session.get(_AUTH) is True


def log_in(request: Request) -> None:
    # A fresh session id on login prevents session fixation and voids pre-login CSRF tokens.
    request.session.clear()
    request.session[_SID] = _new_sid()
    request.session[_AUTH] = True


def log_out(request: Request) -> None:
    request.session.clear()


def _secret(request: Request) -> str:
    return get_state(request).config.session_secret.get_secret_value()


def csrf_token(request: Request) -> str:
    return make_csrf_token(_secret(request), session_id(request))


async def require_login(request: Request) -> None:
    session_id(request)  # Every visitor carries a session id from the first response on.
    if request.url.path not in PUBLIC_PATHS and not is_authenticated(request):
        raise LoginRequired


async def _submitted_token(request: Request) -> str | None:
    token = request.headers.get(CSRF_HEADER)
    if token is not None:
        return token
    field = (await request.form()).get(CSRF_FIELD)
    return field if isinstance(field, str) else None


async def require_csrf(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    token = await _submitted_token(request)
    if token is None or not check_csrf_token(
        _secret(request), session_id(request), token, SESSION_MAX_AGE_S
    ):
        raise CsrfFailed
