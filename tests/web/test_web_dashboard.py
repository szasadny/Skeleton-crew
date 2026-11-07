from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fake_control import FakeRunControl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import Web, open_web

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.store import audit_store, runs_store
from skeleton_crew.store.settings_store import save_settings


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker) as harness:
        await harness.login()
        yield harness


async def _kill_switch_summaries(web: Web) -> list[str]:
    async with web.sessionmaker() as session:
        events = await audit_store.list_events(session, 50, 0, AuditKind.KILL_SWITCH)
    return [event.summary for event in reversed(events)]


async def test_dashboard_shows_idle_state_without_schedule(web: Web) -> None:
    html = (await web.client.get("/")).text
    assert "Idle" in html
    assert "Not scheduled" in html
    assert 'hx-get="/partials/status"' in html
    assert 'hx-trigger="every 5s"' in html
    assert "X-CSRF-Token" in html


async def test_skull_eyes_follow_the_run_state(web: Web) -> None:
    idle = (await web.client.get("/")).text
    assert 'class="skull"' in idle
    assert "hx-swap-oob" not in idle

    web.controller.running = True
    assert 'class="skull active"' in (await web.client.get("/runs")).text
    partial = (await web.client.get("/partials/status")).text
    assert 'class="skull active"' in partial
    assert 'hx-swap-oob="true"' in partial


async def test_dashboard_shows_running_state_and_next_run(web: Web) -> None:
    web.controller.running = True
    web.scheduler.apply(AppSettings(schedule=Schedule(weekdays=frozenset({Weekday.MON}))))
    html = (await web.client.get("/partials/status")).text
    assert "Running" in html
    assert "Not scheduled" not in html


async def test_status_partial_lists_active_tasks_with_pr_link(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", dry_run=False)
        task = await runs_store.add_worker_task(session, run.id, 7, "Fix <b>parser</b>")
        await runs_store.update_worker_task(session, task.id, state=TaskState.CODING, pr_number=42)
        await session.commit()
    html = (await web.client.get("/partials/status", headers={"HX-Request": "true"})).text
    assert "#7 Fix &lt;b&gt;parser&lt;/b&gt;" in html
    assert "coding" in html
    assert 'href="https://github.com/acme/widgets/pull/42"' in html
    assert f'href="/runs/{run.id}"' in html


async def test_run_now_starts_a_manual_run(web: Web) -> None:
    response = await web.post("/actions/run-now")
    assert response.headers["location"] == "/?msg=run_started"
    assert web.controller.starts == ["manual"]
    assert "Run started." in (await web.client.get(response.headers["location"])).text


async def test_run_now_reports_refusal(
    tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with open_web(tmp_path, sessionmaker, FakeRunControl(start_result=None)) as web:
        await web.login()
        response = await web.post("/actions/run-now")
    assert response.headers["location"] == "/?msg=run_refused"


async def test_kill_switch_persists_kills_and_audits(web: Web) -> None:
    response = await web.post("/actions/kill")
    assert response.headers["location"] == "/?msg=killed"
    assert web.controller.kills == 1
    assert (await web.settings()).kill_switch is True
    assert await _kill_switch_summaries(web) == ["kill switch engaged from the UI"]
    assert "Release kill switch" in (await web.client.get("/")).text


async def test_release_kill_switch(web: Web) -> None:
    await web.post("/actions/kill")
    response = await web.post("/actions/release")
    assert response.headers["location"] == "/?msg=released"
    assert (await web.settings()).kill_switch is False
    assert web.controller.kills == 1
    assert (await _kill_switch_summaries(web))[-1] == "kill switch released"


async def test_dry_run_toggle(web: Web) -> None:
    assert (await web.settings()).dry_run is True
    await web.post("/actions/dry-run", {"enabled": "off"})
    assert (await web.settings()).dry_run is False
    await web.post("/actions/dry-run", {"enabled": "on"})
    assert (await web.settings()).dry_run is True


async def test_last_run_summary(web: Web) -> None:
    async with web.sessionmaker() as session:
        await save_settings(session, AppSettings())
        run = await runs_store.create_run(session, "schedule", dry_run=True)
        await runs_store.add_worker_task(session, run.id, 1, "One")
        await session.commit()
    html = (await web.client.get("/")).text
    assert f"Run {run.id}</a>: running, schedule, dry run." in html
    assert "queued 1" in html


async def test_last_run_shows_tasks_with_blockers(web: Web) -> None:
    async with web.sessionmaker() as session:
        await save_settings(session, AppSettings())
        run = await runs_store.create_run(session, "manual", dry_run=False)
        for number in (1, 2, 3):
            task = await runs_store.add_worker_task(session, run.id, number, "T")
            if number < 3:
                await runs_store.update_worker_task(session, task.id, blockers=["worker: x"])
        await session.commit()
    html = (await web.client.get("/partials/status")).text
    assert "2 tasks with blockers" in html


async def test_last_run_without_blockers_shows_no_badge(web: Web) -> None:
    async with web.sessionmaker() as session:
        await save_settings(session, AppSettings())
        run = await runs_store.create_run(session, "manual", dry_run=False)
        await runs_store.add_worker_task(session, run.id, 1, "T")
        await session.commit()
    assert "with blockers" not in (await web.client.get("/")).text
