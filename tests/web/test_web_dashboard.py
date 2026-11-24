from factories import seed_refine, seed_run, seed_task
from fake_control import FakeRunControl
from web_helpers import NOTICE, NOTICE_CLASS, Web, WebFactory, notice

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import AppSettings, Schedule, Weekday
from skeleton_crew.domain.task_state import RefineState, RunStep, RunTrigger, TaskState
from skeleton_crew.web.routes.dashboard import STATUS_POLL_S


async def _kill_switch_summaries(web: Web) -> list[str]:
    return [event.summary for event in await web.audit(AuditKind.KILL_SWITCH)]


async def test_dashboard_shows_idle_state_without_schedule(web: Web) -> None:
    html = (await web.client.get("/")).text
    assert "Idle" in html
    assert "Not scheduled" in html
    assert 'hx-get="/partials/status"' in html
    assert f'hx-trigger="every {STATUS_POLL_S}s"' in html
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
        run = await seed_run(session)
        await seed_task(
            session, run.id, 7, "Fix <b>parser</b>", state=TaskState.CODING, pr_number=42
        )
        await session.commit()
    html = (await web.client.get("/partials/status", headers={"HX-Request": "true"})).text
    assert "#7 Fix &lt;b&gt;parser&lt;/b&gt;" in html
    assert "<td>issue</td>" in html
    assert "coding" in html
    assert 'href="https://github.com/acme/widgets/pull/42"' in html
    assert f'href="/runs/{run.id}"' in html


async def test_status_partial_shows_the_running_selector(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await seed_run(session)
        await session.commit()
    web.controller.running = True
    web.controller.current_step = RunStep.SELECTOR
    html = (await web.client.get("/partials/status")).text
    assert f'<a href="/runs/{run.id}">Run {run.id}</a></td>' in html
    assert "<td>selector</td>" in html
    assert "No active tasks." not in html


async def test_status_partial_hides_a_step_once_idle(web: Web) -> None:
    web.controller.current_step = RunStep.SELECTOR
    html = (await web.client.get("/partials/status")).text
    assert "<td>selector</td>" not in html
    assert "No active tasks." in html


async def test_status_partial_lists_active_refines_only(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await seed_run(session)
        await seed_refine(session, run.id, 5, state=RefineState.REFINING)
        await seed_refine(session, run.id, 6, state=RefineState.DONE)
        await session.commit()
    html = (await web.client.get("/partials/status")).text
    assert "<td>#5</td>" in html
    assert "<td>refine</td>" in html
    assert 'data-state="refining"' in html
    assert "<td>#6</td>" not in html


async def test_run_now_starts_a_manual_run(web: Web) -> None:
    response = await web.post("/actions/run-now")
    assert response.headers["location"] == "/?msg=run_started"
    assert web.controller.starts == ["manual"]
    assert "Run started." in (await web.client.get(response.headers["location"])).text


async def test_run_now_reports_refusal(web_factory: WebFactory) -> None:
    async with web_factory(controller=FakeRunControl(start_result=None)) as web:
        response = await web.post("/actions/run-now")
    assert response.headers["location"] == "/?msg=run_refused"


async def test_kill_switch_persists_kills_and_audits(web: Web) -> None:
    response = await web.post("/actions/kill")
    assert response.headers["location"] == "/?msg=killed"
    assert web.controller.kills == 1
    assert (await web.settings()).kill_switch is True
    assert await _kill_switch_summaries(web) == ["kill switch engaged from the UI"]
    assert "Release kill switch" in (await web.client.get("/")).text


async def test_kill_switch_sits_behind_glass_only_while_armed(web: Web) -> None:
    armed = (await web.client.get("/")).text
    assert 'id="kill-switch" hx-preserve data-break-glass' in armed
    assert 'src="/static/break-glass.js"' in armed
    await web.post("/actions/kill")
    engaged = (await web.client.get("/partials/status")).text
    assert "data-break-glass" not in engaged
    assert 'id="kill-switch"' not in engaged


async def test_release_kill_switch(web: Web) -> None:
    await web.post("/actions/kill")
    response = await web.post("/actions/release")
    assert response.headers["location"] == "/?msg=released"
    assert (await web.settings()).kill_switch is False
    assert web.controller.kills == 1
    assert (await _kill_switch_summaries(web))[-1] == "kill switch released"


async def test_dry_run_toggle(web: Web) -> None:
    assert (await web.settings()).dry_run is True
    response = await web.post("/actions/dry-run", {"enabled": "off"})
    assert response.headers["location"] == "/?msg=dry_run_off"
    assert (await web.settings()).dry_run is False
    await web.post("/actions/dry-run", {"enabled": "on"})
    assert (await web.settings()).dry_run is True


async def test_dry_run_accepts_only_the_exact_on_value(web: Web) -> None:
    await web.post("/actions/dry-run", {"enabled": "off"})
    await web.post("/actions/dry-run", {"enabled": " on"})
    assert (await web.settings()).dry_run is False


async def test_last_run_summary(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await seed_run(session, RunTrigger.SCHEDULE, dry_run=True)
        await seed_task(session, run.id, 1, "One")
        await session.commit()
    html = (await web.client.get("/")).text
    assert f"Run {run.id}</a>: running, schedule, dry run." in html
    assert "queued 1" in html


async def test_last_run_shows_tasks_with_blockers(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await seed_run(session)
        for number in (1, 2):
            await seed_task(session, run.id, number, blockers=["worker: x"])
        await seed_task(session, run.id, 3)
        await session.commit()
    html = (await web.client.get("/partials/status")).text
    assert "2 tasks with blockers" in html


async def test_last_run_without_blockers_shows_no_badge(web: Web) -> None:
    async with web.sessionmaker() as session:
        run = await seed_run(session)
        await seed_task(session, run.id)
        await session.commit()
    assert "with blockers" not in (await web.client.get("/")).text


async def test_sidebar_puts_monitoring_before_configuration(web: Web) -> None:
    html = (await web.client.get("/")).text
    links = [
        "/runs",
        "/blockers",
        "/logs",
        "/repo",
        "/agents",
        "/rules/common",
        "/schedule",
        "/limits",
        "/ci",
    ]
    positions = [html.index(f'<a href="{link}" class="vertebra') for link in links]
    assert positions == sorted(positions)
    monitor, configure = html.index(">Monitor<"), html.index(">Configure<")
    assert monitor < positions[0] < configure < positions[3]
    assert '<span class="segment">M4</span>' in html
    assert '<span class="segment">C6</span>' in html


async def test_sidebar_marks_only_the_current_section(web: Web) -> None:
    html = (await web.client.get("/rules/worker")).text
    assert '<a href="/rules/common" class="vertebra current">' in html
    assert html.count("vertebra current") == 1
    home = (await web.client.get("/")).text
    assert '<a href="/" class="vertebra current">' in home
    assert home.count("vertebra current") == 1


async def test_dashboard_message_sits_between_dry_run_and_kill_switch(web: Web) -> None:
    response = await web.post("/actions/kill")
    html = (await web.client.get(response.headers["location"])).text
    message_at = html.index(f"{NOTICE}Kill switch engaged.")
    assert html.index("dry run</button>") < message_at < html.index("Release kill switch")
    assert html.count(NOTICE_CLASS) == 1
    partial = (await web.client.get("/partials/status")).text
    assert f'<div id="flash" hx-preserve>{notice()}</div>' in partial


async def test_dashboard_message_slot_sits_in_controls_row(web: Web) -> None:
    html = (await web.client.get("/")).text
    slot = html.index(notice())
    assert html.index("dry run</button>") < slot < html.index("Kill switch</button>")
    assert html.count(NOTICE_CLASS) == 1
