import pytest
from fake_agent import ok_outcome
from fake_runtime import ExecCall
from pipeline_helpers import (
    BLOCKER,
    GREETING,
    Harness,
    only_task,
    review_json,
    worker_json,
)

from skeleton_crew.agents.base import failed_outcome
from skeleton_crew.domain.ci_policy import ReviewCheck
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import worker_key
from skeleton_crew.domain.task_state import TaskState
from skeleton_crew.pipeline.sandbox import INBOX
from skeleton_crew.pipeline.sessions import PROMPT_FILE
from skeleton_crew.pipeline.worker_review import SINCE_REVIEW_FILE
from skeleton_crew.runtime.containers import ROLE_LABEL, WORKSPACE

pytestmark = pytest.mark.usefixtures("selected_issue")


async def test_reviewer_runs_in_separate_readonly_container_with_the_diff(
    harness: Harness,
) -> None:
    harness.patches = [harness.patch(GREETING)]
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())

    run_id = await harness.run()

    roles = [spec.labels[ROLE_LABEL] for spec in harness.runtime.started]
    assert roles == ["selector", "worker", "reviewer"]
    worker, reviewer = harness.runtime.started[1], harness.runtime.started[2]
    assert [(m.container_path, m.read_only) for m in worker.mounts] == [
        (WORKSPACE, False),
        (INBOX, True),
    ]
    assert [(m.container_path, m.read_only) for m in reviewer.mounts] == [
        (WORKSPACE, True),
        (INBOX, True),
    ]
    assert worker.mounts[0].host_path != reviewer.mounts[0].host_path
    assert "HTTPS_PROXY" in worker.env
    assert not harness.runtime.running
    assert harness.backend.prompts(Role.REVIEWER)[0].resume_session_id is None
    task = await only_task(harness, run_id)
    root = harness.config.tasks_dir / worker_key(task.id)
    assert sorted(p.name for p in root.iterdir()) == ["transcripts"]
    transcripts = sorted(p.name for p in (root / "transcripts").iterdir())
    assert transcripts == ["reviewer-1.jsonl", "worker-1.jsonl"]
    patch_calls = [c for c in harness.runtime.execs if c.argv[0] == "sh"]
    assert patch_calls[0].env == {}
    assert patch_calls[0].argv[-1] == task.base_sha


def _snapshot_reviewer_mounts(harness: Harness) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    def snapshot(call: ExecCall) -> None:
        if call.argv[1] != Role.REVIEWER.value:
            return
        [spec] = [s for s in harness.runtime.started if s.labels[ROLE_LABEL] == "reviewer"]
        assert spec == harness.spec_of(call)
        workspace, inbox = harness.mount_of(call, WORKSPACE), harness.mount_of(call, INBOX)
        since = inbox / SINCE_REVIEW_FILE
        seen.append(
            {
                "app.py": (workspace / "app.py").read_text(),
                "inbox": " ".join(sorted(p.name for p in inbox.iterdir())),
                "since": since.read_text() if since.exists() else "",
                "prompt": (inbox / PROMPT_FILE).read_text(),
            }
        )

    harness.on_session(snapshot)
    return seen


async def test_second_review_resumes_the_reviewer_in_the_same_container(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print(1)\n"})]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    changes = ok_outcome(review_json("changes_requested", [BLOCKER]), session_id="review-session")
    harness.backend.script(Role.REVIEWER, changes, review_json())
    seen = _snapshot_reviewer_mounts(harness)

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert (task.state, task.review_rounds) == (TaskState.DONE, 1)
    roles = [spec.labels[ROLE_LABEL] for spec in harness.runtime.started]
    assert roles == ["selector", "worker", "reviewer"]
    first, second = harness.backend.prompts(Role.REVIEWER)
    assert first.resume_session_id is None
    assert first.prompt.startswith("# Task: review a change")
    assert second.resume_session_id == "review-session"
    assert second.prompt.startswith("# Task: review the updated change")
    assert [(snap["app.py"], snap["inbox"]) for snap in seen] == [
        (GREETING["app.py"], "changes.diff prompt.md rules.md"),
        ("print(1)\n", "changes.diff prompt.md rules.md since-review.diff"),
    ]
    assert [snap["prompt"] for snap in seen] == [first.prompt, second.prompt]
    assert not harness.runtime.running


async def test_since_review_diff_holds_only_the_new_changes(harness: Harness) -> None:
    harness.patches = [
        harness.patch(GREETING),
        harness.patch({**GREETING, "notes.txt": "second round\n"}),
    ]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(
        Role.REVIEWER, review_json("changes_requested", [BLOCKER]), review_json()
    )
    seen = _snapshot_reviewer_mounts(harness)

    await harness.run()

    first, second = (snap["since"] for snap in seen)
    assert first == ""
    assert "notes.txt" in second
    assert "app.py" not in second


async def test_failed_resumed_review_escalates_and_removes_the_reviewer(harness: Harness) -> None:
    harness.patches = [harness.patch(GREETING), harness.patch({"app.py": "print(1)\n"})]
    harness.backend.script(Role.WORKER, worker_json(), worker_json())
    harness.backend.script(
        Role.REVIEWER,
        review_json("changes_requested", [BLOCKER]),
        failed_outcome("session crashed"),
    )

    run_id = await harness.run()

    task = await only_task(harness, run_id)
    assert task.state is TaskState.ESCALATED
    assert "review session failed: session crashed" in str(task.note)
    assert len(harness.backend.prompts(Role.REVIEWER)) == 2
    assert not harness.runtime.running
    checks = [(w.args["sha"], w.args["state"]) for w in harness.writes("set_review_check")]
    assert checks[-1] == (task.head_sha, ReviewCheck.PENDING)
    assert harness.gateway.review_checks[str(task.head_sha)][0] is ReviewCheck.PENDING
