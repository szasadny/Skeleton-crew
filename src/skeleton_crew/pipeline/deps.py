import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.classifiers.base import Classifier
from skeleton_crew.core.audit import AuditEntry, AuditSink
from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.job_policy import may_start_work
from skeleton_crew.domain.settings import AppSettings, Role
from skeleton_crew.github.gateway import Gateway
from skeleton_crew.runtime.containers import ContainerRuntime
from skeleton_crew.runtime.task_dirs import TaskPaths, task_paths
from skeleton_crew.store import audit_store, settings_store


@dataclass(frozen=True)
class PipelineDeps:
    config: BootstrapConfig
    sessionmaker: async_sessionmaker[AsyncSession]
    gateway_factory: Callable[[AppSettings, AuditSink], Gateway]
    runtime: ContainerRuntime
    backend_factory: Callable[[str, AppSettings], AgentBackend]
    classifier_factory: Callable[[AppSettings], Classifier]
    secret_scanner: Callable[[bytes], Awaitable[list[str]]]
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]

    def gateway(self, settings: AppSettings, run_id: int | None = None) -> Gateway:
        return self.gateway_factory(settings, audit_sink(self.sessionmaker, run_id))

    async def load_settings(self) -> AppSettings:
        async with self.sessionmaker() as session:
            return await settings_store.load_settings(session)

    def task_paths(self, key: str) -> TaskPaths:
        return task_paths(self.config.tasks_dir, key)


@dataclass(frozen=True)
class RunContext:
    deps: PipelineDeps
    settings: AppSettings
    run_id: int
    gateway: Gateway
    backends: Mapping[str, AgentBackend]
    rules: Mapping[Role, str]
    repo_docs: Mapping[str, str]
    deadline: datetime
    audit: AuditSink
    required_checks: frozenset[str]
    classifier: Classifier | None
    docs_clone: TaskPaths
    halted: asyncio.Event = field(default_factory=asyncio.Event)

    def task_paths(self, key: str) -> TaskPaths:
        return self.deps.task_paths(key)

    def session(self) -> AsyncSession:
        return self.deps.sessionmaker()

    def may_start(self) -> bool:
        return may_start_work(self.halted.is_set(), self.deps.clock(), self.deadline)


def audit_sink(sessionmaker: async_sessionmaker[AsyncSession], run_id: int | None) -> AuditSink:
    # Own session and commit per event, so audits survive a later failure of the caller.
    async def record(entry: AuditEntry) -> None:
        async with sessionmaker() as session:
            await audit_store.record(session, replace(entry, run_id=run_id))
            await session.commit()

    return record
