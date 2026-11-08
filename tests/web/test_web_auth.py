from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import PASSWORD, Web, open_web

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.store import audit_store
from skeleton_crew.web.security import SECURITY_HEADERS


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker) as harness:
        yield harness


async def _login_attempt(web: Web, password: str) -> int:
    token = await web.csrf()
    response = await web.client.post("/login", data={"password": password, "csrf_token": token})
    return response.status_code


async def _login_events(web: Web) -> list[tuple[AuditOutcome, str, object]]:
    async with web.sessionmaker() as session:
        events = await audit_store.list_events(session, 50, 0, AuditKind.LOGIN)
    return [(event.outcome, event.summary, event.payload) for event in reversed(events)]


@pytest.mark.parametrize(
    "path", ["/", "/schedule", "/limits", "/agents/draft", "/rules/common", "/runs", "/logs"]
)
async def test_pages_redirect_to_login_without_session(web: Web, path: str) -> None:
    response = await web.client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_htmx_request_without_session_gets_401_with_hx_redirect(web: Web) -> None:
    response = await web.client.get("/partials/status", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


async def test_unauthenticated_post_does_not_reach_the_controller(web: Web) -> None:
    token = await web.csrf()
    response = await web.client.post("/actions/run-now", data={"csrf_token": token})
    assert response.status_code == 303
    assert web.controller.starts == []


async def test_correct_password_logs_in_and_audits(web: Web) -> None:
    await web.login()
    assert (await web.client.get("/")).status_code == 200
    assert await _login_events(web) == [
        (AuditOutcome.ALLOWED, "login succeeded", {"client": "127.0.0.1"})
    ]


async def test_wrong_password_is_rejected_and_audited_without_the_password(web: Web) -> None:
    assert await _login_attempt(web, "wrong password guess") == 401
    events = await _login_events(web)
    assert events == [(AuditOutcome.BLOCKED, "login failed", {"client": "127.0.0.1"})]
    assert "wrong password guess" not in repr(events)
    assert (await web.client.get("/")).status_code == 303


async def test_authenticated_pages_are_never_cached(web: Web) -> None:
    await web.login()
    assert (await web.client.get("/")).headers["Cache-Control"] == "no-store"


async def test_failed_login_blocks_even_the_right_password_until_backoff_ends(web: Web) -> None:
    assert await _login_attempt(web, "nope") == 401
    assert await _login_attempt(web, PASSWORD) == 429
    web.clock.now += 1.1
    assert await _login_attempt(web, PASSWORD) == 303
    outcomes = [summary for _, summary, _ in await _login_events(web)]
    assert outcomes == ["login failed", "login throttled", "login succeeded"]


async def test_backoff_doubles_and_resets_after_success(web: Web) -> None:
    assert await _login_attempt(web, "nope") == 401
    web.clock.now += 1.1
    assert await _login_attempt(web, "nope") == 401
    web.clock.now += 1.1
    assert await _login_attempt(web, PASSWORD) == 429
    web.clock.now += 1.0
    assert await _login_attempt(web, PASSWORD) == 303
    await web.post("/logout")
    assert await _login_attempt(web, "nope") == 401
    web.clock.now += 1.1
    assert await _login_attempt(web, PASSWORD) == 303


async def test_backoff_is_capped_at_thirty_seconds(web: Web) -> None:
    for _ in range(8):
        await _login_attempt(web, "nope")
        web.clock.now += 31
    await _login_attempt(web, "nope")
    web.clock.now += 30.1
    assert await _login_attempt(web, PASSWORD) == 303


async def test_overlong_password_is_rejected(web: Web) -> None:
    assert await _login_attempt(web, "x" * 5000) == 401


async def test_login_without_csrf_token_is_rejected(web: Web) -> None:
    await web.client.get("/login")
    response = await web.client.post("/login", data={"password": PASSWORD})
    assert response.status_code == 403
    assert await _login_events(web) == []


async def test_post_without_csrf_token_is_rejected(web: Web) -> None:
    await web.login()
    response = await web.client.post("/actions/run-now")
    assert response.status_code == 403
    assert web.controller.starts == []


async def test_post_with_forged_csrf_token_is_rejected(web: Web) -> None:
    await web.login()
    token = await web.csrf("/logs")
    response = await web.client.post("/actions/kill", data={"csrf_token": token[:-2] + "xx"})
    assert response.status_code == 403
    assert web.controller.kills == 0


async def test_csrf_token_from_before_login_is_void_after_login(web: Web) -> None:
    stale = await web.csrf()
    await web.login()
    response = await web.client.post("/actions/run-now", data={"csrf_token": stale})
    assert response.status_code == 403


async def test_csrf_token_of_another_session_is_rejected(
    web: Web, tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with open_web(tmp_path, sessionmaker) as other:
        await other.login()
        foreign = await other.csrf("/logs")
    await web.login()
    response = await web.client.post("/actions/run-now", data={"csrf_token": foreign})
    assert response.status_code == 403


async def test_csrf_token_in_header_is_accepted(web: Web) -> None:
    await web.login()
    token = await web.csrf("/logs")
    response = await web.client.post("/actions/run-now", headers={"X-CSRF-Token": token})
    assert response.status_code == 303
    assert web.controller.starts == ["manual"]


async def test_logout_ends_the_session(web: Web) -> None:
    await web.login()
    assert (await web.post("/logout")).headers["location"] == "/login"
    assert (await web.client.get("/")).status_code == 303


@pytest.mark.parametrize("path", ["/login", "/static/app.css", "/does-not-exist"])
async def test_security_headers_on_every_response(web: Web, path: str) -> None:
    response = await web.client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_pages_have_no_inline_scripts_or_styles(web: Web) -> None:
    await web.login()
    for path in ["/", "/schedule", "/limits", "/agents", "/repo", "/rules/worker", "/runs"]:
        html = (await web.client.get(path)).text
        assert "style=" not in html
        assert "<style" not in html
        assert "<script>" not in html
        assert "<script " in html
        assert 'src="/static/htmx.min.js"' in html


async def test_session_cookie_is_secure_and_strict_by_default(
    tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with open_web(tmp_path, sessionmaker, insecure_cookies=False) as web:
        cookie = (await web.client.get("/login")).headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "httponly" in cookie
    assert "max-age=43200" in cookie
