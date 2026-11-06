from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fake_control import FakeRunnerControl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import Web, open_web

from skeleton_crew.domain.runner_policy import RunnerState, RunnerStatus
from skeleton_crew.domain.settings import AppSettings, SelfHostedCi
from skeleton_crew.store.settings_store import save_settings

STATES = [
    RunnerState("runner-1", RunnerStatus.BUSY),
    RunnerState("runner-2", RunnerStatus.OFFLINE, "kill switch is engaged"),
]


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker, runners=FakeRunnerControl(STATES)) as harness:
        await harness.login()
        yield harness


def _form(**overrides: str) -> dict[str, Any]:
    form = {
        "ci.enabled": "on",
        "ci.labels": "skeleton-crew\nlinux",
        "ci.egress_allowlist": "github.com\napi.github.com",
        "ci.no_proxy": "db\nminio",
        "ci.runners": "3",
        "ci.cpus": "4",
        "ci.memory_gb": "8",
        "ci.pids": "4096",
    }
    return form | overrides


async def test_ci_settings_round_trip(web: Web) -> None:
    response = await web.post("/ci", _form())

    assert (response.status_code, response.headers["location"]) == (303, "/ci?msg=saved")
    ci = (await web.settings()).ci
    assert ci == SelfHostedCi(
        enabled=True,
        runners=3,
        labels=("skeleton-crew", "linux"),
        egress_allowlist=("github.com", "api.github.com"),
        no_proxy=("db", "minio"),
        cpus=4,
        memory_gb=8,
        pids=4096,
    )
    html = (await web.client.get("/ci")).text
    assert "--labels skeleton-crew,linux" in html
    assert "https://github.com/acme/widgets" in html


async def test_unchecked_box_turns_self_hosted_ci_off(web: Web) -> None:
    form = _form()
    form.pop("ci.enabled")

    await web.post("/ci", form)

    assert (await web.settings()).ci.enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("ci.labels", "has space"), ("ci.egress_allowlist", "http://evil"), ("ci.runners", "0")],
)
async def test_invalid_ci_settings_are_rejected(web: Web, field: str, value: str) -> None:
    response = await web.post("/ci", _form(**{field: value}))

    assert response.status_code == 422
    assert (await web.settings()).ci == SelfHostedCi()


async def test_ci_page_shows_runner_status(web: Web) -> None:
    html = (await web.client.get("/ci")).text

    assert "runner-1" in html
    assert "busy" in html
    assert "kill switch is engaged" in html


async def test_ci_page_shows_how_to_route_only_skeleton_prs(web: Web) -> None:
    html = (await web.client.get("/ci")).text

    assert "${{ startsWith(github.head_ref, 'skeleton/') &amp;&amp; vars.CI_RUNNER" in html


async def test_ci_post_without_csrf_is_refused(web: Web) -> None:
    response = await web.client.post("/ci", data=_form())

    assert response.status_code == 403
    assert (await web.settings()).ci == SelfHostedCi()


async def test_dashboard_summarises_runners_only_when_enabled(web: Web) -> None:
    assert "CI runners" not in (await web.client.get("/partials/status")).text
    async with web.sessionmaker() as session:
        await save_settings(session, AppSettings(ci=SelfHostedCi(enabled=True)))
        await session.commit()

    html = (await web.client.get("/partials/status")).text

    assert "CI runners" in html
    assert "1 busy, 1 offline" in html
