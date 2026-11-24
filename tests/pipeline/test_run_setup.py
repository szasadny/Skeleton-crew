import pytest
from fake_agent import MODEL_DOMAIN
from pipeline_helpers import (
    ONE_LINER,
    Harness,
    only_task,
    review_json,
    selection_json,
    worker_json,
)

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.ci_policy import BranchRule
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import (
    RunState,
    TaskState,
)
from skeleton_crew.github.gateway import RulesUnavailable
from skeleton_crew.runtime.egress import RECONFIGURE_ARGV


async def test_preflight_failure_stops_the_run(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.runtime.health_problems = ["docker runtime sysbox-runc is not installed"]
    harness.backend.problems = ["model token missing"]
    harness.gateway.ruleset_bypasses[1] = "always"

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "sysbox-runc" in str(run.note)
    assert "model token missing" in str(run.note)
    assert "bypass" in str(run.note)
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
    assert (AuditKind.PREFLIGHT_FAILED, AuditOutcome.BLOCKED) in await harness.audit_kinds()


@pytest.mark.parametrize(
    "update",
    [
        {"agents": {"model": "model-removed-in-a-release"}},
        {"agents": {"effort": "ultra"}},
        {"agents": {"backend": "gone_backend"}},
        {"backend_options": {"claude_code": {"auth_mode": "root"}}},
        {"backend_options": {"claude_code": {"unknown_option": "x"}}},
    ],
)
async def test_invalid_agent_settings_fail_preflight(
    harness: Harness, update: dict[str, dict[str, object]]
) -> None:
    harness.add_ready_issue(1)
    agents = harness.settings.agents
    if "agents" in update:
        worker = agents[Role.WORKER].model_copy(update=update["agents"])
        harness.set(agents={**agents, Role.WORKER: worker})
    else:
        harness.set(**update)

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "agents.worker" in str(run.note) or "backend_options.claude_code" in str(run.note)
    assert harness.backend.requests == []
    assert harness.backend_builds == []
    assert harness.runtime.started == []
    assert harness.gateway.writes == []


async def test_valid_settings_build_each_configured_backend_once(harness: Harness) -> None:
    await harness.run()

    assert harness.backend_builds == ["claude_code"]


async def test_egress_allowlist_includes_backend_domains(harness: Harness) -> None:
    await harness.run()

    allowlist = harness.config.egress_allowlist_path.read_text().split()
    assert MODEL_DOMAIN in allowlist
    assert "pypi.org" in allowlist
    assert any(c.argv == RECONFIGURE_ARGV for c in harness.runtime.execs)


def _require_tests_check(harness: Harness) -> None:
    rule = BranchRule(
        "required_status_checks", 2, {"required_status_checks": [{"context": "tests"}]}
    )
    harness.gateway.rules.append(rule)


async def test_ci_waits_for_the_checks_the_ruleset_requires(harness: Harness) -> None:
    _require_tests_check(harness)
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert harness.gateway.ci_required
    assert set(harness.gateway.ci_required) == {frozenset({"tests"})}


async def test_repo_without_rulesets_waits_for_no_required_checks(harness: Harness) -> None:
    harness.set("repo", allow_unprotected_base_branch=True)
    harness.gateway.failures["branch_rules"] = RulesUnavailable()
    harness.add_ready_issue(1)
    harness.patches = [harness.patch(ONE_LINER)]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    assert set(harness.gateway.ci_required) == {frozenset()}


async def test_unreadable_required_checks_fail_the_run_closed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.add_ready_issue(1)
    real = harness.gateway.branch_rules
    calls = 0

    async def flaky(branch: str) -> list[BranchRule]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise TimeoutError
        return await real(branch)

    monkeypatch.setattr(harness.gateway, "branch_rules", flaky)

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "required checks could not be read" in str(run.note)
    assert harness.runtime.started == []
    assert harness.gateway.writes == []
