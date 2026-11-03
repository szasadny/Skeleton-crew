from collections.abc import AsyncIterator, Mapping
from datetime import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web_helpers import Web, open_web

from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import (
    AppSettings,
    ClaudeAuthMode,
    Effort,
    Limits,
    Role,
    Weekday,
)
from skeleton_crew.store import audit_store


@pytest.fixture
async def web(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Web]:
    async with open_web(tmp_path, sessionmaker) as harness:
        await harness.login()
        yield harness


def _limits_form(**overrides: Any) -> dict[str, str]:
    values = Limits().model_dump() | overrides
    return {f"limits.{k}": "" if v is None else str(v) for k, v in values.items()}


def _agents_form(**overrides: str) -> dict[str, str]:
    form = {}
    for role in Role:
        form[f"agents.{role}.backend"] = "claude_code"
        form[f"agents.{role}.model"] = "opus"
        form[f"agents.{role}.effort"] = "medium"
    form["claude_auth_mode"] = "subscription"
    return form | overrides


def _repo_form(**overrides: str) -> dict[str, str]:
    form = {
        "labels.ready": "skeleton",
        "labels.refine": "skeleton refine",
        "labels.hitl": "skeleton HITL",
        "repo.base_branch": "main",
        "repo.protected_paths": ".github/**",
        "repo.setup_command": "",
        "repo.require_ci": "on",
        "repo.trusted_users": "",
        "runtime.egress_allowlist": "pypi.org",
        "runtime.cpus": "2",
        "runtime.memory_gb": "4",
        "runtime.pids": "2048",
    }
    return form | overrides


async def _post(web: Web, path: str, data: Mapping[str, Any]) -> tuple[int, str]:
    response = await web.post(path, data)
    return response.status_code, response.headers.get("location", response.text)


async def test_schedule_round_trip_applies_scheduler(web: Web) -> None:
    status, location = await _post(
        web,
        "/schedule",
        {
            "schedule.weekdays": ["mon", "wed"],
            "schedule.start_time": "02:30",
            "schedule.window_hours": "5.5",
            "schedule.timezone": "UTC",
        },
    )
    assert (status, location) == (303, "/schedule?msg=saved")
    schedule = (await web.settings()).schedule
    assert schedule.weekdays == frozenset({Weekday.MON, Weekday.WED})
    assert schedule.start_time == time(2, 30)
    assert schedule.window_hours == 5.5
    assert schedule.timezone == "UTC"
    next_run = web.scheduler.next_run_time()
    assert next_run is not None
    assert (next_run.hour, next_run.minute, next_run.weekday() in {0, 2}) == (2, 30, True)
    html = (await web.client.get("/schedule")).text
    assert 'value="mon" checked' in html
    assert 'value="tue" checked' not in html
    assert 'value="02:30"' in html


async def test_schedule_with_no_weekdays_removes_the_job(web: Web) -> None:
    await _post(
        web,
        "/schedule",
        {"schedule.start_time": "01:00", "schedule.window_hours": "6", "schedule.timezone": "UTC"},
    )
    assert (await web.settings()).schedule.weekdays == frozenset()
    assert web.scheduler.next_run_time() is None


async def test_schedule_validation_error_rerenders_form(web: Web) -> None:
    status, html = await _post(
        web,
        "/schedule",
        {
            "schedule.weekdays": ["fri"],
            "schedule.start_time": "03:00",
            "schedule.window_hours": "6",
            "schedule.timezone": "Mars/Olympus",
        },
    )
    assert status == 422
    assert "unknown timezone: Mars/Olympus" in html
    assert 'value="Mars/Olympus"' in html
    assert 'value="fri" checked' in html
    assert (await web.settings()).schedule == AppSettings().schedule


async def test_limits_page_exposes_model_constraints(web: Web) -> None:
    html = (await web.client.get("/limits")).text
    assert 'name="limits.issues_per_day" type="number" step="1" min="0" max="50"' in html
    assert 'name="limits.worker_timeout_hours" type="number" step="any" min="0" max="24"' in html
    assert "Max budget USD per session" in html


async def test_limits_round_trip_and_audit(web: Web) -> None:
    form = _limits_form(issues_per_day=7, max_budget_usd_per_session=12.5)
    assert await _post(web, "/limits", form) == (303, "/limits?msg=saved")
    limits = (await web.settings()).limits
    assert (limits.issues_per_day, limits.max_budget_usd_per_session) == (7, 12.5)
    await _post(web, "/limits", _limits_form(issues_per_day=7))
    assert (await web.settings()).limits.max_budget_usd_per_session is None
    async with web.sessionmaker() as session:
        events = await audit_store.list_events(session, 10, 0, AuditKind.SETTINGS_CHANGED)
    assert events[0].summary == "settings changed: limits"


async def test_limits_validation_error(web: Web) -> None:
    status, html = await _post(web, "/limits", _limits_form(issues_per_day=999))
    assert status == 422
    assert "less than or equal to 50" in html
    assert 'value="999"' in html
    assert (await web.settings()).limits == Limits()


async def test_agents_round_trip(web: Web) -> None:
    form = _agents_form(
        **{
            "agents.worker.model": "sonnet",
            "agents.worker.effort": "high",
            "agents.worker.web_tools": "on",
            "claude_auth_mode": "api_key",
        }
    )
    assert await _post(web, "/agents", form) == (303, "/agents?msg=saved")
    settings = await web.settings()
    worker = settings.agents[Role.WORKER]
    assert (worker.model, worker.effort, worker.web_tools) == ("sonnet", Effort.HIGH, True)
    assert settings.agents[Role.REVIEWER].web_tools is False
    assert settings.claude_auth_mode is ClaudeAuthMode.API_KEY
    assert 'value="api_key" selected' in (await web.client.get("/agents")).text


async def test_agents_validation_error(web: Web) -> None:
    status, html = await _post(
        web, "/agents", _agents_form(**{"agents.refiner.model": "bad model"})
    )
    assert status == 422
    assert "model name contains unsupported characters" in html
    assert (await web.settings()).agents == AppSettings().agents


@pytest.mark.parametrize("model", ["gibberish", "opus5", "claude-opus-9-9"])
async def test_agents_rejects_a_model_the_backend_does_not_offer(web: Web, model: str) -> None:
    status, html = await _post(web, "/agents", _agents_form(**{"agents.worker.model": model}))
    assert status == 422
    assert "not a claude_code model" in html
    assert (await web.settings()).agents[Role.WORKER].model == "opus"


async def test_agents_model_is_a_select_of_backend_models(web: Web) -> None:
    html = (await web.client.get("/agents")).text
    assert '<select id="agents.worker.model"' in html
    assert '<option value="claude-opus-5-5">' in html


async def test_agents_rejects_unknown_backend(web: Web) -> None:
    status, _ = await _post(web, "/agents", _agents_form(**{"agents.worker.backend": "evil"}))
    assert status == 422


async def test_repo_round_trip(web: Web) -> None:
    form = _repo_form(
        **{
            "repo.protected_paths": ".github/**\r\n\r\n  infra/**  \r\n",
            "repo.setup_command": "make deps",
            "repo.trusted_users": "alice\nbob",
            "runtime.egress_allowlist": "pypi.org\n.example.com",
            "runtime.pids": "4096",
        }
    )
    form.pop("repo.require_ci")
    assert await _post(web, "/repo", form) == (303, "/repo?msg=saved")
    settings = await web.settings()
    assert settings.repo.protected_paths == (".github/**", "infra/**")
    assert settings.repo.setup_command == "make deps"
    assert settings.repo.require_ci is False
    assert settings.repo.trusted_users == ("alice", "bob")
    assert settings.runtime.egress_allowlist == ("pypi.org", ".example.com")
    assert settings.runtime.pids == 4096
    html = (await web.client.get("/repo")).text
    assert ".github/**\ninfra/**</textarea>" in html


async def test_repo_validation_error_for_domain(web: Web) -> None:
    status, html = await _post(
        web, "/repo", _repo_form(**{"runtime.egress_allowlist": "pypi.org\nhttp://evil"})
    )
    assert status == 422
    assert "invalid domain: http://evil" in html
    assert (await web.settings()).runtime == AppSettings().runtime


async def test_repo_model_level_error_is_shown_at_the_top(web: Web) -> None:
    status, html = await _post(web, "/repo", _repo_form(**{"labels.hitl": "Skeleton"}))
    assert status == 422
    assert '<div class="alert"><p>label names must be distinct</p></div>' in html
