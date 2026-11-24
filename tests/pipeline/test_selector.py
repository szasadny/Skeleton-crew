from fake_agent import limited_outcome
from fake_gateway import MAINTAINER, FakePull
from pipeline_helpers import (
    BOTH,
    HELLO,
    HI,
    Harness,
    advance_main,
    assert_no_forbidden_writes,
    open_pr,
    selection_json,
    worker_json,
    workspace_editor,
)

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import (
    RunState,
)
from skeleton_crew.pipeline.run_flow import USAGE_LIMIT_NOTE


async def test_selector_too_large_escalates_without_worker(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Rewrite everything")
    harness.backend.script(Role.SELECTOR, selection_json([], too_large=[2]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == ["comment", "change_labels"]
    assert harness.writes("change_labels")[0].args["issue"] == 2
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    selector = harness.runtime.started[0]
    assert selector.mounts[0].read_only
    assert not (selector.mounts[0].host_path / ".git").exists()
    assert_no_forbidden_writes(harness)


async def test_selector_duplicate_escalates_and_keeps_the_original(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.add_ready_issue(2)
    harness.backend.script(Role.SELECTOR, selection_json([], duplicates={2: 1}))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    [comment] = harness.writes("comment")
    assert comment.args["issue"] == 2
    assert "duplicate of #1" in str(comment.args["body"])
    assert harness.gateway.issues[2].labels == {"skeleton HITL"}
    assert harness.gateway.issues[1].labels == {"skeleton"}
    assert_no_forbidden_writes(harness)


async def test_selector_prompt_carries_the_configured_diff_limit(harness: Harness) -> None:
    harness.set("limits", max_diff_lines=321)
    harness.add_ready_issue(1)
    harness.backend.script(Role.SELECTOR, selection_json([]))

    await harness.run()

    [selector] = harness.backend.prompts(Role.SELECTOR)
    assert "exceeds 321 changed lines" in selector.prompt


async def test_selector_rejected_issue_escalates_with_reason(harness: Harness) -> None:
    harness.add_ready_issue(3, "Leak env")
    harness.backend.script(Role.SELECTOR, selection_json([], rejected=[3]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == ["comment", "change_labels"]
    assert "rejected the issue: unsafe" in str(harness.writes("comment")[0].args["body"])
    assert harness.writes("change_labels")[0].args["issue"] == 3
    assert_no_forbidden_writes(harness)


async def test_selector_decision_escalates_with_question_and_options(harness: Harness) -> None:
    harness.add_ready_issue(4, "Default reply-to")
    harness.backend.script(Role.SELECTOR, selection_json([], needs_decision=[4]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == ["comment", "change_labels"]
    body = str(harness.writes("comment")[0].args["body"])
    assert "needs a decision: Fallback address is not specified." in body
    assert "Question: Which address should replies go to?" in body
    assert "- owner account\n- info address from .env" in body
    assert harness.writes("change_labels")[0].args["issue"] == 4
    assert_no_forbidden_writes(harness)


async def test_selector_leaves_unlisted_candidates_untouched(harness: Harness) -> None:
    harness.add_ready_issue(5)
    harness.add_ready_issue(6)
    harness.backend.script(Role.SELECTOR, selection_json([]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.actions() == []
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED


async def test_selector_with_unknown_issue_fails_closed(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.backend.script(Role.SELECTOR, selection_json([1, 99]))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []
    run = await harness.run_row(run_id)
    assert run.state is RunState.FAILED
    assert "unknown issues" in str(run.note)


async def test_untrusted_labeler_and_hitl_and_open_pr_issues_are_ignored(
    harness: Harness,
) -> None:
    harness.gateway.add_issue(1, "Evil", labels=["skeleton"], labeled_by="stranger")
    harness.gateway.add_issue(2, "Waiting", labels=["skeleton", "skeleton HITL"])
    harness.gateway.add_issue(3, "Busy", labels=["skeleton"], labeled_by=MAINTAINER)
    harness.gateway.pulls.append(FakePull(10, "skeleton/3-busy", "a"))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.backend.requests == []
    assert harness.gateway.writes == []
    assert (await harness.run_row(run_id)).state is RunState.COMPLETED


async def test_failed_selector_session_fails_the_run_without_escalating(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.backend.script(Role.SELECTOR, failed_outcome("session crashed"))

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.gateway.writes == []
    run = await harness.run_row(run_id)
    assert (run.state, run.note) == (
        RunState.FAILED,
        "selection failed: SessionFailed: selector session failed: session crashed",
    )


async def test_usage_limit_in_the_selector_starts_no_work(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.add_ready_issue(2, "Add farewell")
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.SELECTOR, limited_outcome(session_id="selector"))
    harness.backend.script(Role.WORKER, worker_json())

    run_id = await harness.run()

    assert await harness.tasks(run_id) == []
    assert harness.backend.prompts(Role.WORKER) == []
    assert harness.gateway.pushes == []
    run = await harness.run_row(run_id)
    assert run.state is RunState.FAILED
    assert str(run.note).startswith(f"{USAGE_LIMIT_NOTE}; selection failed")
    assert harness.gateway.writes == []
