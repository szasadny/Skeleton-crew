import asyncio
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.agents.registry import settings_problems
from skeleton_crew.agents.rules import compose_rules, effective_rules
from skeleton_crew.classifiers import registry as classifiers
from skeleton_crew.classifiers.base import Classifier
from skeleton_crew.domain.ci_policy import required_checks
from skeleton_crew.domain.quota_policy import local_midnight, remaining
from skeleton_crew.domain.repo_docs import render_repo_docs, select_docs
from skeleton_crew.domain.settings import COMMON_RULES, AppSettings, Role
from skeleton_crew.domain.task_keys import selector_key
from skeleton_crew.domain.task_state import TaskKind
from skeleton_crew.github.gateway import Gateway, RulesUnavailable
from skeleton_crew.github.preflight import check_repository
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.pipeline.runner_pool import runner_problems
from skeleton_crew.pipeline.sandbox import agent_target
from skeleton_crew.runtime.egress import apply_allowlist
from skeleton_crew.runtime.repo_docs_reader import read_repo_docs
from skeleton_crew.runtime.task_dirs import TaskPaths
from skeleton_crew.store import rules_store, runs_store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quotas:
    issues: int
    refines: int
    fixes: int


@dataclass(frozen=True)
class RepoDocs:
    by_backend: dict[str, str]
    clone: TaskPaths


@dataclass(frozen=True)
class Preflight:
    problems: list[str]
    backends: dict[str, AgentBackend]
    classifier: Classifier | None


def make_backends(deps: PipelineDeps, settings: AppSettings) -> dict[str, AgentBackend]:
    names = sorted({agent.backend for agent in settings.agents.values()})
    return {name: deps.backend_factory(name, settings) for name in names}


async def preflight(deps: PipelineDeps, settings: AppSettings, gw: Gateway) -> Preflight:
    problems = settings_problems(settings) + classifiers.settings_problems(settings)
    # Backends are built only from settings the registry accepts.
    backends = {} if problems else make_backends(deps, settings)
    classifier = None
    if not problems and settings.classifier.enabled:
        classifier = deps.classifier_factory(settings)
        problems += classifiers.credential_problems(
            settings.classifier.provider, deps.config.agent_credentials
        )
    problems += await _environment_problems(deps, settings, gw)
    for backend in backends.values():
        problems += backend.credential_problems()
    return Preflight(problems, backends, classifier)


async def _environment_problems(
    deps: PipelineDeps, settings: AppSettings, gw: Gateway
) -> list[str]:
    config = deps.config
    problems: list[str] = []
    try:
        problems += await check_repository(
            gw,
            base_branch=settings.repo.base_branch,
            allow_unprotected=settings.repo.allow_unprotected_base_branch,
        )
        problems += await deps.runtime.health(agent_target(config))
        problems += [p for p in await runner_problems(deps, settings, gw) if p not in problems]
    except Exception as exc:
        log.exception("preflight check failed")
        return [*problems, f"preflight check failed: {type(exc).__name__}"]
    return problems


async def load_required_checks(gw: Gateway, base_branch: str) -> frozenset[str]:
    try:
        return required_checks(await gw.branch_rules(base_branch))
    except RulesUnavailable:
        return frozenset()


async def apply_egress(
    deps: PipelineDeps, settings: AppSettings, backends: Mapping[str, AgentBackend]
) -> None:
    domains = set(settings.runtime.egress_allowlist)
    for backend in backends.values():
        domains.update(backend.egress_domains())
    await apply_allowlist(deps.runtime, domains, agent_target(deps.config))


async def remaining_quotas(session: AsyncSession, settings: AppSettings, now: datetime) -> Quotas:
    since = local_midnight(now, settings.schedule.timezone)
    workers = await runs_store.count_worker_tasks_since(session, since)
    refines = await runs_store.count_refine_tasks_since(session, since)
    fixes = await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX)
    limits = settings.limits
    return Quotas(
        remaining(limits.issues_per_day, workers),
        remaining(limits.refines_per_day, refines),
        remaining(limits.conflict_fixes_per_day, fixes),
    )


async def load_rules(session: AsyncSession) -> dict[Role, str]:
    common = effective_rules(COMMON_RULES, await rules_store.active_rules(session, COMMON_RULES))
    rules: dict[Role, str] = {}
    for role in Role:
        own = effective_rules(role.value, await rules_store.active_rules(session, role.value))
        rules[role] = compose_rules(common, own)
    return rules


async def collect_repo_docs(
    deps: PipelineDeps, gw: Gateway, run_id: int, backends: Mapping[str, AgentBackend]
) -> RepoDocs:
    paths = deps.task_paths(selector_key(run_id))
    clone = paths.controller_repo
    try:
        await gw.clone(clone)
        sources = await asyncio.to_thread(read_repo_docs, clone)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, clone, ignore_errors=True)
        raise
    docs = {
        name: render_repo_docs(
            select_docs(sources.candidates, sources.texts, backend.auto_loaded_docs())
        )
        for name, backend in backends.items()
    }
    return RepoDocs(docs, paths)
