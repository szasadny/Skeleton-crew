import asyncio
import logging
import secrets
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.session_policy import workspace_writable
from skeleton_crew.domain.settings import Role
from skeleton_crew.domain.task_keys import container_name
from skeleton_crew.pipeline.deps import RunContext
from skeleton_crew.runtime.containers import (
    CONTAINER_PREFIX,
    ROLE_LABEL,
    TASK_LABEL,
    WORKSPACE,
    ContainerHandle,
    ContainerSpec,
    Mount,
    SandboxTarget,
    proxy_env,
)
from skeleton_crew.runtime.controller_git import head_commit, make_readonly_copy
from skeleton_crew.runtime.task_dirs import TaskPaths

INBOX = "/skeleton-in"
RULES_FILE = "rules.md"
REVIEW_DIR = "review"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sandbox:
    handle: ContainerHandle
    role: Role
    inbox: Path
    transcripts: Path


def agent_target(config: BootstrapConfig) -> SandboxTarget:
    return SandboxTarget(
        image=config.agent_image,
        network=config.agent_network,
        proxy_url=config.egress_proxy_url,
        proxy_container=config.egress_proxy_container,
        allowlist_path=config.egress_allowlist_path,
    )


def container_spec(ctx: RunContext, key: str, role: Role, paths: TaskPaths) -> ContainerSpec:
    target, limits = agent_target(ctx.deps.config), ctx.settings.runtime
    return ContainerSpec(
        name=container_name(CONTAINER_PREFIX, key, role.value, secrets.token_hex(4)),
        image=target.image,
        network=target.network,
        mounts=[
            Mount(paths.agent_repo, WORKSPACE, read_only=not workspace_writable(role)),
            Mount(paths.inbox, INBOX, read_only=True),
        ],
        env=proxy_env(target.proxy_url),
        cpus=limits.cpus,
        memory_gb=limits.memory_gb,
        pids=limits.pids,
        labels={TASK_LABEL: key, ROLE_LABEL: role.value},
    )


def write_inbox(inbox: Path, rules: str, files: Mapping[str, bytes]) -> None:
    if inbox.exists():
        shutil.rmtree(inbox)
    inbox.mkdir(parents=True)
    (inbox / RULES_FILE).write_text(rules, encoding="utf-8")
    update_inbox(inbox, files)


def update_inbox(inbox: Path, files: Mapping[str, bytes]) -> None:
    for name, content in files.items():
        (inbox / name).write_bytes(content)


def _rules(ctx: RunContext, role: Role) -> str:
    rules = ctx.rules[role]
    docs = ctx.repo_docs[ctx.settings.agents[role].backend]
    return f"{rules}\n{docs}" if docs else rules


async def start_sandbox(
    ctx: RunContext,
    key: str,
    role: Role,
    paths: TaskPaths,
    files: Mapping[str, bytes] | None = None,
) -> Sandbox:
    await asyncio.to_thread(write_inbox, paths.inbox, _rules(ctx, role), files or {})
    handle = await ctx.deps.runtime.start(container_spec(ctx, key, role, paths))
    return Sandbox(handle, role, paths.inbox, paths.transcripts)


async def remove_sandbox(ctx: RunContext, sandbox: Sandbox | None) -> None:
    if sandbox is None:
        return
    try:
        await ctx.deps.runtime.remove(sandbox.handle)
    except Exception:
        log.exception("could not remove container %s", sandbox.handle.name)


@asynccontextmanager
async def sandbox(
    ctx: RunContext,
    key: str,
    role: Role,
    paths: TaskPaths,
    *,
    files: Mapping[str, bytes] | None = None,
) -> AsyncIterator[Sandbox]:
    box = await start_sandbox(ctx, key, role, paths, files)
    try:
        yield box
    finally:
        await remove_sandbox(ctx, box)


async def replace_clone_with_readonly_copy(paths: TaskPaths) -> None:
    sha = await head_commit(paths.controller_repo)
    await asyncio.to_thread(make_readonly_copy, paths.controller_repo, paths.agent_repo, sha)
    await asyncio.to_thread(shutil.rmtree, paths.controller_repo, ignore_errors=True)


@asynccontextmanager
async def readonly_sandbox(
    ctx: RunContext, key: str, role: Role, paths: TaskPaths
) -> AsyncIterator[Sandbox]:
    await replace_clone_with_readonly_copy(paths)
    async with sandbox(ctx, key, role, paths) as box:
        yield box


def review_paths(paths: TaskPaths) -> TaskPaths:
    root = paths.root / REVIEW_DIR
    return TaskPaths(
        root=root,
        controller_repo=paths.controller_repo,
        agent_repo=root / "workspace",
        inbox=root / "inbox",
        transcripts=paths.transcripts,
    )


def remove_work_dirs(paths: TaskPaths) -> None:
    review = review_paths(paths).root
    for path in (paths.agent_repo, paths.inbox, paths.controller_repo, review):
        shutil.rmtree(path, ignore_errors=True)


@asynccontextmanager
async def work_dirs(paths: TaskPaths) -> AsyncIterator[TaskPaths]:
    try:
        yield paths
    finally:
        await asyncio.to_thread(remove_work_dirs, paths)
