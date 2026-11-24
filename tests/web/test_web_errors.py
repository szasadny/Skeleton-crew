from web_helpers import Web

from skeleton_crew.web.security import CSRF_FIELD


async def test_unknown_page_is_an_html_404(web: Web) -> None:
    response = await web.client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Not found</h1>" in response.text
    assert 'class="vertebra' in response.text


async def test_anonymous_404_shows_no_navigation(anon_web: Web) -> None:
    response = await anon_web.client.get("/static/missing.css")
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text
    assert 'class="vertebra' not in response.text


async def test_missing_csrf_token_is_an_html_403(web: Web) -> None:
    response = await web.client.post("/actions/run-now", data={CSRF_FIELD: "forged"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Invalid or missing CSRF token." in response.text


async def test_other_http_errors_keep_their_status(web: Web) -> None:
    response = await web.client.delete("/runs")
    assert response.status_code == 405


async def test_invalid_settings_rerender_the_form_as_422(web: Web) -> None:
    response = await web.post("/schedule", {"schedule.timezone": "Mars/Olympus"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Schedule</h1>" in response.text
    assert "unknown timezone: Mars/Olympus" in response.text
