import pytest
from web_helpers import PASSWORD, Web, WebFactory

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.web.security import (
    CSRF_FIELD,
    CSRF_HEADER,
    LOGIN_PATH,
    SECURITY_HEADERS,
    SESSION_MAX_AGE_S,
)


async def _login_attempt(web: Web, password: str) -> int:
    token = await web.csrf()
    response = await web.client.post(LOGIN_PATH, data={"password": password, CSRF_FIELD: token})
    return response.status_code


async def _login_events(web: Web) -> list[tuple[AuditOutcome, str, object]]:
    return [(e.outcome, e.summary, e.payload) for e in await web.audit(AuditKind.LOGIN)]


@pytest.mark.parametrize(
    "path",
    ["/", "/schedule", "/limits", "/agents/draft", "/rules/common", "/runs", "/blockers", "/logs"],
)
async def test_pages_redirect_to_login_without_session(anon_web: Web, path: str) -> None:
    response = await anon_web.client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_htmx_request_without_session_gets_401_with_hx_redirect(anon_web: Web) -> None:
    response = await anon_web.client.get("/partials/status", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


async def test_unauthenticated_post_does_not_reach_the_controller(anon_web: Web) -> None:
    token = await anon_web.csrf()
    response = await anon_web.client.post("/actions/run-now", data={CSRF_FIELD: token})
    assert response.status_code == 303
    assert anon_web.controller.starts == []


async def test_correct_password_logs_in_and_audits(anon_web: Web) -> None:
    await anon_web.login()
    assert (await anon_web.client.get("/")).status_code == 200
    assert await _login_events(anon_web) == [
        (AuditOutcome.ALLOWED, "login succeeded", {"client": "127.0.0.1"})
    ]


async def test_wrong_password_is_rejected_and_audited_without_the_password(anon_web: Web) -> None:
    assert await _login_attempt(anon_web, "wrong password guess") == 401
    events = await _login_events(anon_web)
    assert events == [(AuditOutcome.BLOCKED, "login failed", {"client": "127.0.0.1"})]
    assert "wrong password guess" not in repr(events)
    assert (await anon_web.client.get("/")).status_code == 303


async def test_authenticated_pages_are_never_cached(web: Web) -> None:
    assert (await web.client.get("/")).headers["Cache-Control"] == "no-store"


async def test_failed_login_blocks_even_the_right_password_until_backoff_ends(
    anon_web: Web,
) -> None:
    assert await _login_attempt(anon_web, "nope") == 401
    assert await _login_attempt(anon_web, PASSWORD) == 429
    anon_web.clock.now += 1.1
    assert await _login_attempt(anon_web, PASSWORD) == 303
    outcomes = [summary for _, summary, _ in await _login_events(anon_web)]
    assert outcomes == ["login failed", "login throttled", "login succeeded"]


async def test_throttled_login_says_how_long_to_wait(anon_web: Web) -> None:
    await _login_attempt(anon_web, "nope")
    anon_web.clock.now += 0.2
    token = await anon_web.csrf()
    response = await anon_web.client.post(
        LOGIN_PATH, data={"password": PASSWORD, CSRF_FIELD: token}
    )
    assert '<div class="alert"><p>Too many failed attempts. Try again in 1 s.</p></div>' in (
        response.text
    )


async def test_backoff_doubles_and_resets_after_success(anon_web: Web) -> None:
    assert await _login_attempt(anon_web, "nope") == 401
    anon_web.clock.now += 1.1
    assert await _login_attempt(anon_web, "nope") == 401
    anon_web.clock.now += 1.1
    assert await _login_attempt(anon_web, PASSWORD) == 429
    anon_web.clock.now += 1.0
    assert await _login_attempt(anon_web, PASSWORD) == 303
    await anon_web.post("/logout")
    assert await _login_attempt(anon_web, "nope") == 401
    anon_web.clock.now += 1.1
    assert await _login_attempt(anon_web, PASSWORD) == 303


async def test_backoff_is_capped_at_thirty_seconds(anon_web: Web) -> None:
    for _ in range(8):
        await _login_attempt(anon_web, "nope")
        anon_web.clock.now += 31
    await _login_attempt(anon_web, "nope")
    anon_web.clock.now += 30.1
    assert await _login_attempt(anon_web, PASSWORD) == 303


async def test_overlong_password_is_rejected(anon_web: Web) -> None:
    assert await _login_attempt(anon_web, "x" * 5000) == 401


async def test_missing_password_is_rejected(anon_web: Web) -> None:
    token = await anon_web.csrf()
    response = await anon_web.client.post(LOGIN_PATH, data={CSRF_FIELD: token})
    assert response.status_code == 401
    assert "Wrong password." in response.text


async def test_login_without_csrf_token_is_rejected(anon_web: Web) -> None:
    await anon_web.client.get(LOGIN_PATH)
    response = await anon_web.client.post(LOGIN_PATH, data={"password": PASSWORD})
    assert response.status_code == 403
    assert await _login_events(anon_web) == []


async def test_post_without_csrf_token_is_rejected(web: Web) -> None:
    response = await web.client.post("/actions/run-now")
    assert response.status_code == 403
    assert web.controller.starts == []


async def test_post_with_forged_csrf_token_is_rejected(web: Web) -> None:
    token = await web.csrf("/logs")
    response = await web.client.post("/actions/kill", data={CSRF_FIELD: token[:-2] + "xx"})
    assert response.status_code == 403
    assert web.controller.kills == 0


async def test_csrf_token_from_before_login_is_void_after_login(anon_web: Web) -> None:
    stale = await anon_web.csrf()
    await anon_web.login()
    response = await anon_web.client.post("/actions/run-now", data={CSRF_FIELD: stale})
    assert response.status_code == 403


async def test_csrf_token_of_another_session_is_rejected(web: Web, web_factory: WebFactory) -> None:
    async with web_factory() as other:
        foreign = await other.csrf("/logs")
    response = await web.client.post("/actions/run-now", data={CSRF_FIELD: foreign})
    assert response.status_code == 403


async def test_empty_csrf_header_is_not_replaced_by_the_form_token(web: Web) -> None:
    token = await web.csrf("/logs")
    response = await web.client.post(
        "/actions/run-now", headers={CSRF_HEADER: ""}, data={CSRF_FIELD: token}
    )
    assert response.status_code == 403
    assert web.controller.starts == []


async def test_csrf_token_in_header_is_accepted(web: Web) -> None:
    token = await web.csrf("/logs")
    response = await web.client.post("/actions/run-now", headers={CSRF_HEADER: token})
    assert response.status_code == 303
    assert web.controller.starts == ["manual"]


async def test_logout_ends_the_session(web: Web) -> None:
    assert (await web.post("/logout")).headers["location"] == "/login"
    assert (await web.client.get("/")).status_code == 303


@pytest.mark.parametrize("path", ["/login", "/static/app.css", "/does-not-exist"])
async def test_security_headers_on_every_response(anon_web: Web, path: str) -> None:
    response = await anon_web.client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_pages_have_no_inline_scripts_or_styles(web: Web) -> None:
    for path in ["/", "/schedule", "/limits", "/agents", "/repo", "/rules/worker", "/runs"]:
        html = (await web.client.get(path)).text
        assert "style=" not in html
        assert "<style" not in html
        assert "<script>" not in html
        assert "<script " in html
        assert 'src="/static/htmx.min.js"' in html


async def test_anonymous_redirect_already_carries_a_session_cookie(anon_web: Web) -> None:
    response = await anon_web.client.get("/")
    assert response.status_code == 303
    assert "set-cookie" in response.headers


async def test_session_cookie_is_secure_and_strict_by_default(web_factory: WebFactory) -> None:
    async with web_factory(login=False, insecure_cookies=False) as web:
        cookie = (await web.client.get(LOGIN_PATH)).headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "samesite=strict" in cookie
    assert "httponly" in cookie
    assert f"max-age={SESSION_MAX_AGE_S}" in cookie
