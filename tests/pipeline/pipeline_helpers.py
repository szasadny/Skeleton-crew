import asyncio
import json
import os
import shutil
import subprocess
from collections import defaultdict, deque
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fake_agent import ok_outcome
from fake_gateway import FakeGitHubGateway, FakePull, WriteRecord
from fake_runtime import ExecCall, FakeRuntime, argv_starts_with, ok
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.agents.base import AgentBackend, SessionOutcome, SessionRequest, failed_outcome
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.settings import AppSettings, Limits, RepoPolicy, Role
from skeleton_crew.domain.task_state import RunState, TaskState
from skeleton_crew.github.gateway import CiState, CiStatus, Gateway
from skeleton_crew.github.write_guard import AuditSink
from skeleton_crew.models.refine_task import RefineTask
from skeleton_crew.models.run import Run
from skeleton_crew.models.worker_task import WorkerTask
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.pipeline.run_controller import RunController
from skeleton_crew.pipeline.sessions import WORKSPACE
from skeleton_crew.runtime.containers import ExecResult
from skeleton_crew.runtime.workspace import AGENT_PATCH_ARGV
from skeleton_crew.store import audit_store, runs_store, settings_store

MODEL_TOKEN = "model-token-for-tests"  # noqa: S105
START = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "HOME": os.devnull,
    "PATH": os.environ.get("PATH", ""),
}


def git(cwd: Path, *args: str) -> bytes:
    completed = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=GIT_ENV,
        check=True,
        capture_output=True,
    )
    return completed.stdout


def make_source_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# Demo\n")
    (root / "app.py").write_text("def main() -> None:\n    pass\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
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


class ScriptedBackend:
    name = "scripted"

    def __init__(self) -> None:
        self.requests: list[SessionRequest] = []
        self.problems: list[str] = []
        self._queues: dict[Role, deque[SessionOutcome | str]] = defaultdict(deque)

    def script(self, role: Role, *items: SessionOutcome | str) -> None:
        self._queues[role].extend(items)

    def egress_domains(self) -> tuple[str, ...]:
        return ("model.example.com",)

    def auto_loaded_docs(self) -> tuple[str, ...]:
        return ("CLAUDE.md", ".claude/CLAUDE.md")

    def credential_problems(self) -> list[str]:
        return list(self.problems)

    def env(self) -> dict[str, str]:
        return {"MODEL_TOKEN": MODEL_TOKEN}

    def command(self, req: SessionRequest) -> list[str]:
        self.requests.append(req)
        return ["fake-agent", req.role.value]

    def parse(self, stdout_path: Path, exit_code: int, timed_out: bool) -> SessionOutcome:
        queue = self._queues[Role(stdout_path.read_text())]
        if not queue:
            return failed_outcome("no scripted outcome")
        item = queue.popleft()
        return ok_outcome(item, session_id="worker-session") if isinstance(item, str) else item

    def prompts(self, role: Role) -> list[SessionRequest]:
        return [r for r in self.requests if r.role is role]


class PipelineGateway(FakeGitHubGateway):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ci_queue: list[CiStatus] = [CiStatus(CiState.SUCCESS, [])]

    def bind(self, settings: AppSettings, audit: AuditSink) -> Gateway:
        self.dry_run = settings.dry_run
        self.labels = settings.labels
        self._external_audit = audit
        return self

    async def ci_status(self, sha: str, required: Collection[str] = ()) -> CiStatus:
        self.ci_required.append(frozenset(required))
        return self.ci_queue.pop(0) if len(self.ci_queue) > 1 else self.ci_queue[0]


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_config(data_dir: Path) -> BootstrapConfig:
    return BootstrapConfig.model_validate(
        {
            "repo": "acme/widgets",
            "github_app_id": 1,
            "github_app_private_key_path": "/nonexistent/key.pem",
            "github_bot_login": "skeleton-crew[bot]",
            "ui_password_hash": "$argon2id$v=19$m=65536,t=3,p=4$fake",
            "session_secret": "s" * 32,
            "data_dir": data_dir,
        }
    )


def worker_json(
    status: str = "done",
    *,
    summary: str = "Implemented the change.",
    pr_title: str = "Add greeting",
    note: str | None = None,
    new_issues: Sequence[dict[str, str]] = (),
    objections: Sequence[dict[str, object]] = (),
) -> str:
    return json.dumps(
        {
            "status": status,
            "summary": summary,
            "pr_title": pr_title,
            "note": note,
            "new_issues": list(new_issues),
            "objections": list(objections),
        }
    )


def is_repo_command(argv: Sequence[str]) -> bool:
    return len(argv) == 3 and argv[:2] == ("sh", "-c") and argv[2].startswith("exec 2>&1\n")


def command_result(exit_code: int = 0, output: bytes = b"") -> ExecResult:
    return ExecResult(
        exit_code=exit_code, stdout_path=None, stdout=output, stderr=b"", timed_out=False
    )


def review_json(
    verdict: str = "approve",
    findings: Sequence[dict[str, object]] = (),
    missing_docs: Sequence[dict[str, object]] = (),
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "summary": "Reviewed.",
            "findings": list(findings),
            "missing_doc_updates": list(missing_docs),
        }
    )


def selection_json(
    picks: Sequence[int] = (), too_large: Sequence[int] = (), rejected: Sequence[int] = ()
) -> str:
    return json.dumps(
        {
            "picks": [{"issue_number": n, "reason": "small"} for n in picks],
            "too_large": [{"issue_number": n, "reason": "huge"} for n in too_large],
            "rejected": [{"issue_number": n, "reason": "unsafe"} for n in rejected],
        }
    )


def refine_json() -> str:
    return json.dumps({"spec_markdown": "Goal: greet.", "open_questions": ["Which language?"]})


BLOCKER = {"severity": "blocker", "path": "app.py", "line": 1, "message": "Handle empty input."}


class Harness:
    def __init__(self, tmp_path: Path, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.tmp_path = tmp_path
        self.sessionmaker = sessionmaker
        self.config = make_config(tmp_path / "data")
        self.source = make_source_repo(tmp_path / "source")
        self.remote = make_bare_repo(tmp_path / "remote.git")
        self.patch = PatchMaker(self.source, tmp_path / "scratch")
        self.gateway = PipelineGateway(source_repo=self.source, push_remote=self.remote)
        self.runtime = FakeRuntime()
        self.backend = ScriptedBackend()
        self.backend_builds: list[str] = []
        self.clock = FakeClock()
        self.secret_findings: list[str] = []
        self.patches: list[bytes] = []
        self.patch_from_workspace: Callable[[ExecCall], bytes] | None = None
        self.sleeps: list[float] = []
        self.sleep_hook: asyncio.Event | None = None
        self.sleeping = asyncio.Event()
        self.settings = AppSettings(dry_run=False)
        self._script_runtime()
        self.controller = RunController(self.deps())

    def _script_runtime(self) -> None:
        self.runtime.script(argv_starts_with(*AGENT_PATCH_ARGV), self._next_patch)
        self.runtime.script(argv_starts_with("fake-agent"), self._session)
        self.runtime.script(argv_starts_with("squid"), ok())

    def _next_patch(self, call: ExecCall) -> Any:
        if self.patch_from_workspace is not None:
            return ok(self.patch_from_workspace(call))
        return ok(self.patches.pop(0) if len(self.patches) > 1 else self.patches[0])

    def _session(self, call: ExecCall) -> Any:
        return ok(call.argv[1].encode())

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
            secret_scanner=self._scan,
            clock=self.clock,
            sleep=self._sleep,
        )

    def limits(self, **values: Any) -> None:
        self.settings = self.settings.model_copy(
            update={
                "limits": Limits.model_validate({**self.settings.limits.model_dump(), **values})
            }
        )

    def repo(self, **values: Any) -> None:
        self.settings = self.settings.model_copy(
            update={
                "repo": RepoPolicy.model_validate({**self.settings.repo.model_dump(), **values})
            }
        )

    def add_ready_issue(self, number: int = 1, title: str = "Add greeting", **kw: Any) -> None:
        self.gateway.add_issue(number, title, body="Print a greeting.", labels=["skeleton"], **kw)

    async def save_settings(self) -> None:
        async with self.sessionmaker() as session:
            await settings_store.save_settings(session, self.settings)
            await session.commit()

    async def run(self) -> int:
        await self.save_settings()
        run_id = await self.controller.start("manual")
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

    async def audit_kinds(self) -> list[tuple[AuditKind, AuditOutcome]]:
        async with self.sessionmaker() as session:
            events = await audit_store.list_events(session, 500, 0)
        return [(e.kind, e.outcome) for e in reversed(events)]

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
        bot_added = {
            e.label for e in issue.label_events if e.added and e.actor == "skeleton-crew[bot]"
        }
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
    for call in harness.runtime.execs:
        if call.argv[0] != "fake-agent":
            assert MODEL_TOKEN not in call.env.values()


BRANCH = "skeleton/1-add-greeting"
PR_NUMBER = 10
HI = "def main() -> None:\n    print('hi')\n"
HELLO = "def main() -> None:\n    print('hello')\n"
BOTH = "def main() -> None:\n    print('hi')\n    print('hello')\n"


def commit_files(repo: Path, files: Mapping[str, str], message: str) -> str:
    for path, content in files.items():
        (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


async def record_push(
    h: Harness,
    head: str,
    *,
    state: TaskState = TaskState.DONE,
    dry_run: bool = False,
) -> None:
    async with h.sessionmaker() as session:
        run = await runs_store.create_run(session, "manual", dry_run)
        task = await runs_store.add_worker_task(session, run.id, 1, "Add greeting")
        await runs_store.update_worker_task(
            session, task.id, state=state, head_sha=head, branch=BRANCH
        )
        await runs_store.finish_run(session, run.id, RunState.COMPLETED)
        await session.commit()


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
    return git(h.remote, "rev-list", "--parents", "-n", "1", sha).decode().split()[1:]


def remote_file(h: Harness, sha: str, path: str) -> str:
    return git(h.remote, "show", f"{sha}:{path}").decode()


def workspace_editor(h: Harness, *rounds: Mapping[str, str]) -> None:
    pending = list(rounds)

    def patch(call: ExecCall) -> bytes:
        spec = next(s for s in h.runtime.started if s.name == call.handle.name)
        workspace = next(m.host_path for m in spec.mounts if m.container_path == WORKSPACE)
        files = pending.pop(0) if len(pending) > 1 else pending[0]
        for path, content in files.items():
            (workspace / path).write_text(content)
        git(workspace, "add", "-A")
        return git(workspace, "diff", "--cached", "--binary", call.argv[-1])

    h.patch_from_workspace = patch


async def only_task(h: Harness, run_id: int) -> WorkerTask:
    [task] = await h.tasks(run_id)
    return task


async def push_branch_without_pr(
    h: Harness, files: Mapping[str, str], *, recorded: bool = True
) -> str:
    work = h.tmp_path / "left-over"
    git(h.tmp_path, "clone", "-q", str(h.source), str(work))
    if files:
        commit_files(work, files, "Earlier work")
    head = git(work, "rev-parse", "HEAD").decode().strip()
    git(work, "push", "-q", str(h.remote), f"{head}:refs/heads/{BRANCH}")
    if recorded:
        await record_push(h, head)
    h.backend.script(Role.SELECTOR, selection_json([1]))
    return head
