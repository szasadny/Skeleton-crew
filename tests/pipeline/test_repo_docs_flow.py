from pathlib import Path

import pytest
from pipeline_helpers import (
    BOTH,
    HELLO,
    HI,
    Harness,
    advance_main,
    commit_files,
    open_pr,
    refine_json,
    review_json,
    selection_json,
    worker_json,
    workspace_editor,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.agents.base import SessionOutcome
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import selector_key
from skeleton_crew.domain.task_state import RunState, TaskKind, TaskState
from skeleton_crew.pipeline.sessions import INBOX, ROLE_LABEL, RULES_FILE

DOCS = {
    "CLAUDE.md": "AUTO-LOADED ROOT\n",
    ".claude/CLAUDE.md": "AUTO-LOADED DOT. See [arch](./ARCHITECTURE.md).\n",
    ".claude/ARCHITECTURE.md": "ARCH: layers go down.\n",
    "AGENTS.md": "AGENTS: run the tests.\n",
}


@pytest.fixture
def harness(tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> Harness:
    h = Harness(tmp_path, sessionmaker)
    (h.source / ".claude").mkdir()
    commit_files(h.source, DOCS, "Add docs")
    return h


def _snapshot_rules(harness: Harness) -> dict[Role, list[str]]:
    seen: dict[Role, list[str]] = {}
    parse = harness.backend.parse

    def snapshot(stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        role = Role(stdout_path.read_text())
        spec = [s for s in harness.runtime.started if s.labels[ROLE_LABEL] == role.value][-1]
        inbox = next(m.host_path for m in spec.mounts if m.container_path == INBOX)
        seen.setdefault(role, []).append((inbox / RULES_FILE).read_text())
        return parse(stdout_path, exit_code, timed_out)

    harness.backend.parse = snapshot  # type: ignore[method-assign]
    return seen


def _assert_docs_appended(rules: str) -> None:
    assert rules.startswith("# Skeleton Crew work rules")
    section = rules.index("# Repository docs")
    assert rules.index("AGENTS: run the tests.") > section
    assert rules.index("ARCH: layers go down.") > section
    assert "AUTO-LOADED" not in rules


async def test_every_role_gets_the_repo_docs_after_its_rules(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.gateway.add_issue(5, "Idea", body="Maybe greet?", labels=["skeleton refine"])
    harness.patches = [harness.patch({"app.py": HI})]
    harness.backend.script(Role.SELECTOR, selection_json([1]))
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())
    harness.backend.script(Role.REFINER, refine_json())
    seen = _snapshot_rules(harness)

    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    assert set(seen) == {Role.SELECTOR, Role.WORKER, Role.REVIEWER, Role.REFINER}
    for rules in (r for texts in seen.values() for r in texts):
        _assert_docs_appended(rules)


async def test_conflict_fix_worker_gets_the_repo_docs(harness: Harness) -> None:
    harness.add_ready_issue(1)
    await open_pr(harness, {"app.py": HI})
    advance_main(harness, {"app.py": HELLO})
    workspace_editor(harness, {"app.py": BOTH})
    harness.backend.script(Role.WORKER, worker_json())
    harness.backend.script(Role.REVIEWER, review_json())
    seen = _snapshot_rules(harness)

    run_id = await harness.run()

    [task] = await harness.tasks(run_id)
    assert (task.kind, task.state) == (TaskKind.PR_FIX, TaskState.DONE)
    for rules in seen[Role.WORKER] + seen[Role.REVIEWER]:
        _assert_docs_appended(rules)


async def test_selector_reuses_the_docs_clone(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.backend.script(Role.SELECTOR, selection_json([], rejected=[1]))
    clones: list[Path] = []
    clone = harness.gateway.clone

    async def counting(dest: Path) -> str:
        clones.append(dest)
        return await clone(dest)

    harness.gateway.clone = counting  # type: ignore[method-assign]

    run_id = await harness.run()

    selector = harness.config.tasks_dir / selector_key(run_id)
    assert clones == [selector / "controller"]
    assert not (selector / "controller").exists()
    assert not (selector / "agent").exists()


async def test_docs_clone_is_removed_when_nothing_is_selected(harness: Harness) -> None:
    run_id = await harness.run()

    assert (await harness.run_row(run_id)).state is RunState.COMPLETED
    assert not (harness.config.tasks_dir / selector_key(run_id) / "controller").exists()
    assert harness.runtime.started == []


async def test_unreadable_repo_docs_fail_the_run_closed(harness: Harness) -> None:
    harness.add_ready_issue(1)
    harness.gateway.source_repo = harness.tmp_path / "missing"

    run_id = await harness.run()

    run = await harness.run_row(run_id)
    assert run.state is RunState.PREFLIGHT_FAILED
    assert "repository docs could not be read" in str(run.note)
    assert harness.runtime.started == []
    assert not (harness.config.tasks_dir / selector_key(run_id)).exists()


async def test_repo_without_docs_gets_the_rules_only(harness: Harness) -> None:
    for path in DOCS:
        (harness.source / path).unlink()
    commit_files(harness.source, {}, "Drop docs")
    harness.add_ready_issue(1)
    harness.backend.script(Role.SELECTOR, selection_json([], rejected=[1]))
    seen = _snapshot_rules(harness)

    await harness.run()

    [rules] = seen[Role.SELECTOR]
    assert "# Repository docs" not in rules
