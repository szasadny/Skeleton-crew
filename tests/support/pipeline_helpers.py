import asyncio
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from audit_helpers import audit_events
from clocks import FakeClock
from configs import make_config
from factories import seed_run, seed_task
from fake_agent import FAKE_AGENT, MODEL_TOKEN, FakeBackend
from fake_classifier import FakeClassifier
from fake_gateway import BOT_LOGIN, FakeGitHubGateway, FakePull, WriteRecord
from fake_runtime import ExecCall, FakeRuntime, argv_starts_with, exec_result
from git_helpers import commit_files, git, git_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sticky import sticky_next

from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.ci_policy import CiState, CiStatus
from skeleton_crew.domain.settings import AppSettings, Role
from skeleton_crew.domain.task_state import RunState, RunTrigger, TaskState
from skeleton_crew.models.audit_event import AuditEvent
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.pipeline.run_controller import RunController
from skeleton_crew.runtime.containers import WORKSPACE, ContainerSpec, ExecResult
from skeleton_crew.runtime.controller_git import AGENT_PATCH_ARGV
from skeleton_crew.runtime.egress import RECONFIGURE_ARGV
from skeleton_crew.store import runs_store, settings_store

CLASSIFIER_KEY = "classifier-key-for-tests"
BRANCH = "skeleton/1-add-greeting"
PR_NUMBER = 10
HI = "def main() -> None:\n    print('hi')\n"
HELLO = "def main() -> None:\n    print('hello')\n"
BOTH = "def main() -> None:\n    print('hi')\n    print('hello')\n"
GREETING = {"app.py": HI}
ONE_LINER = {"app.py": "print('hi')\n"}
BLOCKER = {"severity": "blocker", "path": "app.py", "line": 1, "message": "Handle empty input."}

type SessionHook = Callable[[ExecCall], object]


def make_source_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    commit_files(
        root, {"README.md": "# Demo\n", "app.py": "def main() -> None:\n    pass\n"}, "init"
    )
    return root


def make_bare_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "--bare")
    return root


class PatchMaker:
    def __init__(self, source: Path, scratch: Path) -> None:
        self._source = source
        self._scratch = scratch
        self._count = 0

    def __call__(self, files: dict[str, str]) -> bytes:
        self._count += 1
        work = self._scratch / f"patch-{self._count}"
        shutil.copytree(self._source, work)
        for path, content in files.items():
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(work, "add", "-A")
        return git(work, "diff", "--cached", "--binary", "HEAD")


def worker_json(status: str = "done", **fields: object) -> str:
    output: dict[str, object] = {
        "status": status,
        "summary": "Implemented the change.",
        "pr_title": "Add greeting",
        "note": None,
        "new_issues": [],
        "objections": [],
        "blockers": [],
        "unverified": [],
    }
    unknown = set(fields) - set(output)
    assert not unknown, unknown
    return json.dumps(output | fields)


def is_repo_command(argv: Sequence[str]) -> bool:
    return len(argv) == 3 and argv[:2] == ("sh", "-c") and argv[2].startswith("exec 2>&1\n")


def review_json(
    verdict: str = "approve",
    findings: Sequence[dict[str, object]] = (),
    missing_docs: Sequence[dict[str, object]] = (),
    blockers: Sequence[str] = (),
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "summary": "Reviewed.",
            "findings": list(findings),
            "missing_doc_updates": list(missing_docs),
            "blockers": list(blockers),
        }
    )


def selection_json(
    picks: Sequence[int] = (),
    too_large: Sequence[int] = (),
    rejected: Sequence[int] = (),
    needs_decision: Sequence[int] = (),
    duplicates: Mapping[int, int] | None = None,
) -> str:
    return json.dumps(
        {
            "picks": [{"issue_number": n, "reason": "small"} for n in picks],
            "too_large": [{"issue_number": n, "reason": "huge"} for n in too_large],
            "needs_decision": [
                {
                    "issue_number": n,
                    "problem": "Fallback address is not specified.",
                    "question": "Which address should replies go to?",
                    "options": ["owner account", "info address from .env"],
                }
                for n in needs_decision
            ],
            "rejected": [{"issue_number": n, "reason": "unsafe"} for n in rejected],
            "duplicates": [
                {"issue_number": n, "duplicate_of": kept, "reason": "same change"}
                for n, kept in (duplicates or {}).items()
            ],
        }
    )


def refine_json() -> str:
    return json.dumps({"spec_markdown": "Goal: greet.", "open_questions": ["Which language?"]})


class Harness:
    def __init__(self, tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.tmp_path = tmp_path
        self.sessionmaker = sessionmaker
        self.config = make_config(
            tmp_path / "data",
            github_bot_login=BOT_LOGIN,
            agent_credentials={"typesafe_api_key": CLASSIFIER_KEY},
        )
        self.source = make_source_repo(tmp_path / "source")
        self.remote = make_bare_repo(tmp_path / "remote.git")
        self.patch = PatchMaker(self.source, tmp_path / "scratch")
        self.gateway = FakeGitHubGateway(source_repo=self.source, push_remote=self.remote)
        self.gateway.ci_default = [CiStatus(CiState.SUCCESS, [])]
        self.runtime = FakeRuntime()
        self.backend = FakeBackend()
        self.backend_builds: list[str] = []
        self.classifier = FakeClassifier()
        self.clock = FakeClock()
        self.secret_findings: list[str] = []
        self.patches: list[bytes] = []
        self.patch_from_workspace: Callable[[ExecCall], bytes] | None = None
        self.session_seconds = 0.0
        self.sleeps: list[float] = []
        self.sleep_hook: asyncio.Event | None = None
        self.sleeping = asyncio.Event()
        self.settings = AppSettings(dry_run=False)
        self._session_hooks: list[SessionHook] = []
        self._script_runtime()
        self.controller = RunController(self.deps())

    def _script_runtime(self) -> None:
        self.runtime.script(argv_starts_with(*AGENT_PATCH_ARGV), self._next_patch)
        self.runtime.script(argv_starts_with(FAKE_AGENT), self._session)
        self.runtime.script(argv_starts_with(*RECONFIGURE_ARGV), exec_result())

    def _next_patch(self, call: ExecCall) -> ExecResult:
        if self.patch_from_workspace is not None:
            return exec_result(stdout=self.patch_from_workspace(call))
        return exec_result(stdout=sticky_next(self.patches))

    def _session(self, call: ExecCall) -> ExecResult:
        for hook in self._session_hooks:
            hook(call)
        self.clock.advance(self.session_seconds)
        return exec_result(stdout=call.argv[1].encode())

    async def _scan(self, patch: bytes) -> list[str]:
        return list(self.secret_findings)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        self.sleeping.set()
        if self.sleep_hook is not None:
            await self.sleep_hook.wait()
        await asyncio.sleep(0)

    def _backend(self, name: str, settings: AppSettings) -> AgentBackend:
        self.backend_builds.append(name)
        return self.backend

    def deps(self) -> PipelineDeps:
        return PipelineDeps(
            config=self.config,
            sessionmaker=self.sessionmaker,
            gateway_factory=self.gateway.bind,
            runtime=self.runtime,
            backend_factory=self._backend,
            classifier_factory=lambda settings: self.classifier,
            secret_scanner=self._scan,
            clock=self.clock,
            sleep=self._sleep,
        )

    def set(self, section: str | None = None, /, **values: Any) -> None:
        if section is not None:
            current = getattr(self.settings, section)
            values = {section: type(current).model_validate(current.model_dump() | values)}
        self.settings = self.settings.model_copy(update=values)

    def on_session(self, hook: SessionHook) -> None:
        self._session_hooks.append(hook)

    def spec_of(self, call: ExecCall) -> ContainerSpec:
        return next(s for s in self.runtime.started if s.name == call.handle.name)

    def mount_of(self, call: ExecCall, container_path: str) -> Path:
        mounts = self.spec_of(call).mounts
        return next(m.host_path for m in mounts if m.container_path == container_path)

    def repo_commands(self, *results: tuple[int, bytes]) -> list[ExecCall]:
        pending = list(results)
        calls: list[ExecCall] = []

        def respond(call: ExecCall) -> ExecResult:
            calls.append(call)
            return exec_result(*sticky_next(pending))

        self.runtime.script(is_repo_command, respond)
        return calls

    def add_ready_issue(self, number: int = 1, title: str = "Add greeting", **kw: Any) -> None:
        self.gateway.add_issue(number, title, body="Print a greeting.", labels=["skeleton"], **kw)

    async def save_settings(self) -> None:
        async with self.sessionmaker() as session:
            await settings_store.save_settings(session, self.settings)
            await session.commit()

    async def run(self) -> int:
        await self.save_settings()
        run_id = await self.controller.start(RunTrigger.MANUAL)
        assert run_id is not None
        await self.controller.wait()
        return run_id

    async def run_row(self, run_id: int) -> Run:
        async with self.sessionmaker() as session:
            run = await runs_store.get_run(session, run_id)
        assert run is not None
        return run

    async def tasks(self, run_id: int) -> list[WorkerTask]:
        async with self.sessionmaker() as session:
            return await runs_store.list_tasks(session, run_id)

    async def refines(self, run_id: int) -> list[RefineTask]:
        async with self.sessionmaker() as session:
            return await runs_store.list_refine_tasks(session, run_id)

    async def audit_events(self, kind: AuditKind | None = None) -> list[AuditEvent]:
        async with self.sessionmaker() as session:
            return await audit_events(session, kind)

    async def audit_kinds(self) -> list[tuple[AuditKind, AuditOutcome]]:
        return [(e.kind, e.outcome) for e in await self.audit_events()]

    def actions(self) -> list[str]:
        return [w.action for w in self.gateway.writes]

    def writes(self, action: str) -> list[WriteRecord]:
        return [w for w in self.gateway.writes if w.action == action]


def assert_no_forbidden_writes(harness: Harness) -> None:
    labels = harness.settings.labels
    for write in harness.writes("change_labels"):
        add, remove = write.args["add"], write.args["remove"]
        assert isinstance(add, list)
        assert isinstance(remove, list)
        assert set(add) <= {labels.hitl}
        assert set(remove) <= {labels.ready, labels.refine}
    for issue in harness.gateway.issues.values():
        bot_added = {e.label for e in issue.label_events if e.added and e.actor == BOT_LOGIN}
        assert bot_added <= {labels.hitl}
    assert {w.action for w in harness.gateway.writes} <= {
        "change_labels",
        "comment",
        "create_issue",
        "open_pr",
        "set_review_check",
        "push_branch",
    }
    for spec in harness.runtime.started:
        assert MODEL_TOKEN not in spec.env.values()
        assert CLASSIFIER_KEY not in spec.env.values()
    for call in harness.runtime.execs:
        assert CLASSIFIER_KEY not in call.env.values()
        assert CLASSIFIER_KEY not in " ".join(call.argv)
        if call.argv[0] != FAKE_AGENT:
            assert MODEL_TOKEN not in call.env.values()


async def only_task(h: Harness, run_id: int) -> WorkerTask:
    [task] = await h.tasks(run_id)
    return task


async def record_push(
    h: Harness,
    head: str,
    *,
    state: TaskState = TaskState.DONE,
    dry_run: bool = False,
) -> None:
    async with h.sessionmaker.begin() as session:
        run = await seed_run(session, dry_run=dry_run)
        await seed_task(
            session, run.id, 1, "Add greeting", state=state, head_sha=head, branch=BRANCH
        )
        await runs_store.finish_run(session, run.id, RunState.COMPLETED)


async def open_pr(
    h: Harness,
    files: Mapping[str, str],
    *,
    state: TaskState = TaskState.DONE,
    recorded_head: str | None = None,
    mergeable: bool | None = False,
) -> str:
    work = h.tmp_path / "branch-work"
    git(h.tmp_path, "clone", "-q", str(h.source), str(work))
    git(work, "checkout", "-q", "-b", BRANCH)
    head = commit_files(work, files, "Add greeting")
    git(work, "push", "-q", str(h.remote), f"HEAD:refs/heads/{BRANCH}")
    h.gateway.pulls.append(FakePull(PR_NUMBER, BRANCH, head))
    h.gateway.mergeable[PR_NUMBER] = [mergeable]
    await record_push(h, recorded_head or head, state=state)
    return head


def advance_main(h: Harness, files: Mapping[str, str]) -> str:
    return commit_files(h.source, files, "Change main")


def parents_of(h: Harness, sha: str) -> list[str]:
    return git_text(h.remote, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def remote_file(h: Harness, sha: str, path: str) -> str:
    return git(h.remote, "show", f"{sha}:{path}").decode()


def workspace_editor(h: Harness, *rounds: Mapping[str, str]) -> None:
    pending = list(rounds)

    def patch(call: ExecCall) -> bytes:
        workspace = h.mount_of(call, WORKSPACE)
        for path, content in sticky_next(pending).items():
            (workspace / path).write_text(content)
        git(workspace, "add", "-A")
        return git(workspace, "diff", "--cached", "--binary", call.argv[-1])

    h.patch_from_workspace = patch


async def push_branch_without_pr(
    h: Harness, files: Mapping[str, str], *, recorded: bool = True
) -> str:
    work = h.tmp_path / "left-over"
    git(h.tmp_path, "clone", "-q", str(h.source), str(work))
    if files:
        commit_files(work, files, "Earlier work")
    head = git_text(work, "rev-parse", "HEAD")
    git(work, "push", "-q", str(h.remote), f"{head}:refs/heads/{BRANCH}")
    if recorded:
        await record_push(h, head)
    h.backend.script(Role.SELECTOR, selection_json([1]))
    return head
