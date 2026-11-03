import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from skeleton_crew.core.security import check_csrf_token, make_csrf_token

SESSION_MAX_AGE_S = 12 * 60 * 60
LOGIN_PATH = "/login"
_SID = "sid"
_AUTH = "auth"
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0

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


@dataclass
class _Failures:
    count: int
    blocked_until: float


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._failures: dict[str, _Failures] = {}

    def retry_after(self, client: str) -> float:
        entry = self._failures.get(client)
        if entry is None:
            return 0.0
        return max(0.0, entry.blocked_until - self.clock())

    def failed(self, client: str) -> None:
        entry = self._failures.get(client)
        count = 1 if entry is None else entry.count + 1
        delay = min(_BACKOFF_MAX_S, _BACKOFF_START_S * 2 ** (count - 1))
        self._failures[client] = _Failures(count, self.clock() + delay)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


class LoginRequired(Exception):
    pass


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session_id(request: Request) -> str:
    sid = request.session.get(_SID)
    if not isinstance(sid, str):
        sid = secrets.token_urlsafe(32)
        request.session[_SID] = sid
    return sid


def is_authenticated(request: Request) -> bool:
    return request.session.get(_AUTH) is True


def log_in(request: Request) -> None:
    # A fresh session id on login prevents session fixation and voids pre-login CSRF tokens.
    request.session.clear()
    request.session[_SID] = secrets.token_urlsafe(32)
    request.session[_AUTH] = True


def log_out(request: Request) -> None:
    request.session.clear()


def csrf_token(request: Request, secret: str) -> str:
    return make_csrf_token(secret, session_id(request))


async def verify_request(request: Request, secret: str) -> None:
    sid = session_id(request)
    if request.url.path != LOGIN_PATH and not is_authenticated(request):
        raise LoginRequired
    if request.method in {"GET", "HEAD"}:
        return
    token = request.headers.get("x-csrf-token")
    if token is None:
        token_field = (await request.form()).get("csrf_token")
        token = token_field if isinstance(token_field, str) else None
    if token is None or not check_csrf_token(secret, sid, token, SESSION_MAX_AGE_S):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
