import pytest
from pipeline_helpers import (
    BLOCKER,
    GREETING,
    Harness,
    only_task,
    review_json,
    worker_json,
)

from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_state import TaskState

pytestmark = pytest.mark.usefixtures("selected_issue")


async def test_blockers_reach_the_pr_body_the_review_checks_and_the_task(
    harness: Harness,
) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"README.md": "Greets.\n"})]
    harness.backend.script(
        Role.WORKER,
        worker_json(blockers=["Python 3.13 is missing", "Python 3.13 is missing"]),
        worker_json(blockers=["Python 3.13 is missing", "pypi.org is blocked"]),
    )
    harness.backend.script(
        Role.REVIEWER,
        review_json("changes_requested", findings=[BLOCKER], blockers=["Could not run tests"]),
        review_json(blockers=["Could not run tests"]),
    )

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.DONE
    assert task.blockers == [
        "worker: Python 3.13 is missing",
        "reviewer: Could not run tests",
        "worker: pypi.org is blocked",
    ]
    [pr] = harness.writes("open_pr")
    assert str(pr.args["body"]).endswith("### Blockers\n\n- Python 3.13 is missing")
    _, first, _, second = (str(w.args["summary"]) for w in harness.writes("set_review_check"))
    assert first.endswith(
        "### Blockers\n\n- worker: Python 3.13 is missing\n- reviewer: Could not run tests"
    )
    assert "- worker: pypi.org is blocked" in second
    assert "- reviewer: Could not run tests" in second


async def test_no_blockers_leave_the_task_and_texts_clean(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.blockers is None
    [pr] = harness.writes("open_pr")
    _, check = harness.writes("set_review_check")
    assert "Blockers" not in str(pr.args["body"])
    assert "Blockers" not in str(check.args["summary"])


async def test_unverified_checks_are_never_reported_as_blockers(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json(unverified=["Full test suite not run"]))
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.blockers) == (TaskState.DONE, None)
    [pr] = harness.writes("open_pr")
    _, check = harness.writes("set_review_check")
    for text in (str(pr.args["body"]), str(check.args["summary"])):
        assert "Blockers" not in text
        assert "Full test suite not run" not in text


async def test_blockers_of_a_worker_that_gives_up_are_recorded(harness: Harness) -> None:
    harness.backend.script(
        Role.WORKER, worker_json("blocked", note="No toolchain.", blockers=["Node 22 is missing"])
    )

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert task.blockers == ["worker: Node 22 is missing"]
