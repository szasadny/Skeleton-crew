import pytest
from fake_agent import limited_outcome
from pipeline_helpers import (
    Harness,
    assert_no_forbidden_writes,
    refine_json,
)

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.core.audit import AuditKind
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import (
    RefineState,
    RunState,
)
from skeleton_crew.pipeline.outcomes import USAGE_LIMIT_REASON
from skeleton_crew.pipeline.run_flow import USAGE_LIMIT_NOTE


async def test_refine_flow_comments_and_swaps_labels(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", body="Maybe greet?", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, refine_json())

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert refine.state is RefineState.DONE
    assert harness.actions() == ["comment", "change_labels"]
    [comment] = harness.writes("comment")
    assert str(comment.args["body"]).startswith("## Refined spec\n\nGoal: greet.")
    assert "Which language?" in str(comment.args["body"])
    assert harness.gateway.issues[5].labels == {"skeleton HITL"}
    assert harness.runtime.started[0].mounts[0].read_only
    assert_no_forbidden_writes(harness)


async def test_invalid_refine_output_fails_without_writes(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, '{"spec_markdown": ""}')

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert refine.state is RefineState.FAILED
    assert harness.gateway.writes == []


async def test_usage_limit_in_a_refine_starts_no_further_refine(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.gateway.add_issue(6, "Other idea", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, limited_outcome(session_id="refine"), refine_json())

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert refine.state is RefineState.ABORTED
    assert len(harness.backend.prompts(Role.REFINER)) == 1
    assert harness.gateway.writes == []
    assert harness.gateway.issues[5].labels == {"skeleton refine"}
    assert harness.gateway.issues[6].labels == {"skeleton refine"}
    run = await harness.run_row(run_id)
    assert (run.state, run.note) == (RunState.FAILED, USAGE_LIMIT_NOTE)


async def test_usage_limit_in_a_refine_aborts_it_without_escalating(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, limited_outcome(session_id="refine"))

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert (refine.state, refine.error) == (RefineState.ABORTED, USAGE_LIMIT_REASON)
    assert refine.cost_usd == pytest.approx(0.03)
    assert harness.gateway.writes == []
    assert harness.gateway.issues[5].labels == {"skeleton refine"}
    assert AuditKind.TASK_ESCALATED not in {k for k, _ in await harness.audit_kinds()}
    run = await harness.run_row(run_id)
    assert (run.state, run.note) == (RunState.FAILED, USAGE_LIMIT_NOTE)


async def test_failed_refine_session_fails_the_refine_only(harness: Harness) -> None:
    harness.gateway.add_issue(5, "Idea", labels=["skeleton refine"])
    harness.backend.script(Role.REFINER, failed_outcome("session crashed"))

    run_id = await harness.run()

    [refine] = await harness.refines(run_id)
    assert (refine.state, refine.error) == (
        RefineState.FAILED,
        "refiner session failed: session crashed",
    )
    assert harness.gateway.writes == []
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
