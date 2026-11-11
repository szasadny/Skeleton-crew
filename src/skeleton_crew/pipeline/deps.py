import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.classifiers.base import Classifier
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.config import BootstrapConfig
from skeleton_crew.domain.settings import AppSettings, Role
from skeleton_crew.github.gateway import Gateway
from skeleton_crew.github.write_guard import AuditSink
from skeleton_crew.runtime.containers import ContainerRuntime
from skeleton_crew.store import audit_store


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
    halted: asyncio.Event = field(default_factory=asyncio.Event)


def audit_sink(sessionmaker: async_sessionmaker[AsyncSession], run_id: int | None) -> AuditSink:
    # Own session and commit per event, so audits survive a later failure of the caller.
    async def record(
        kind: AuditKind,
        outcome: AuditOutcome,
        summary: str,
        payload: Mapping[str, object] | None,
    ) -> None:
        async with sessionmaker() as session:
            await audit_store.record(session, kind, outcome, summary, payload, run_id=run_id)
            await session.commit()

    return record
