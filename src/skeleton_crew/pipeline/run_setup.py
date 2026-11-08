import asyncio
import logging
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.agents.base import AgentBackend
from skeleton_crew.agents.registry import settings_problems
from skeleton_crew.agents.rules import compose_rules, default_rules
from skeleton_crew.classifiers import registry as classifiers
from skeleton_crew.classifiers.base import Classifier
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.repo_docs import render_repo_docs, select_docs
from skeleton_crew.domain.settings import AppSettings, Role
from skeleton_crew.domain.task_keys import selector_key
from skeleton_crew.domain.task_state import TaskKind
from skeleton_crew.domain.trust_policy import IssueComment
from skeleton_crew.github.gateway import (
    Gateway,
    IssueRef,
    PullRef,
    RulesUnavailable,
    required_checks,
)
from skeleton_crew.github.preflight import check_repository
from skeleton_crew.github.trust import trusted_issues
from skeleton_crew.github.write_guard import AuditSink
from skeleton_crew.pipeline.deps import PipelineDeps
from skeleton_crew.pipeline.runner_pool import runner_problems
from skeleton_crew.runtime.egress import apply_allowlist
from skeleton_crew.runtime.workspace import read_repo_docs, task_paths
from skeleton_crew.store import rules_store, runs_store

COMMON_RULES = "common"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    issue: IssueRef
    comments: Sequence[IssueComment]


@dataclass(frozen=True)
class OpenPr:
    issue: IssueRef
    comments: Sequence[IssueComment]
    pull: PullRef


@dataclass(frozen=True)
class Candidates:
    ready: list[Candidate]
    refine: list[Candidate]
    open_prs: list[OpenPr]


@dataclass(frozen=True)
class Quotas:
    issues: int
    refines: int
    fixes: int


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
        report = await check_repository(
            gw,
            base_branch=settings.repo.base_branch,
            allow_unprotected=settings.repo.allow_unprotected_base_branch,
        )
        problems += report.problems
        problems += await deps.runtime.health(
            image=config.agent_image,
            network=config.agent_network,
            proxy_container=config.egress_proxy_container,
        )
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
    await apply_allowlist(
        deps.runtime,
        domains,
        deps.config.egress_allowlist_path,
        deps.config.egress_proxy_container,
    )


def _has_label(issue: IssueRef, label: str) -> bool:
    return any(name.casefold() == label.casefold() for name in issue.labels)


async def find_candidates(
    deps: PipelineDeps, settings: AppSettings, gw: Gateway, audit: AuditSink
) -> Candidates:
    labels, trusted = settings.labels, settings.repo.trusted_users
    bot = deps.config.github_bot_login
    ready: list[Candidate] = []
    open_prs: list[OpenPr] = []
    pulls_by_issue = await gw.open_skeleton_prs()
    for issue, comments in await trusted_issues(gw, labels.ready, trusted, bot):
        if _has_label(issue, labels.hitl) or _has_label(issue, labels.refine):
            continue
        match pulls_by_issue.get(issue.number, ()):
            case ():
                ready.append(Candidate(issue, comments))
            case (pull,):
                open_prs.append(OpenPr(issue, comments, pull))
            case pulls:
                await _skip_ambiguous(audit, issue.number, pulls)
    refine = [
        Candidate(issue, comments)
        for issue, comments in await trusted_issues(gw, labels.refine, trusted, bot)
        if not _has_label(issue, labels.hitl)
    ]
    return Candidates(ready, refine, open_prs)


async def _skip_ambiguous(audit: AuditSink, issue_number: int, pulls: Sequence[PullRef]) -> None:
    numbers = sorted(pull.number for pull in pulls)
    await audit(
        AuditKind.ISSUE_SKIPPED,
        AuditOutcome.INFO,
        f"#{issue_number} has {len(numbers)} open skeleton PRs",
        {"issue": issue_number, "prs": numbers},
    )


def local_midnight(now: datetime, timezone: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def remaining_quotas(session: AsyncSession, settings: AppSettings, now: datetime) -> Quotas:
    since = local_midnight(now, settings.schedule.timezone)
    workers = await runs_store.count_worker_tasks_since(session, since)
    refines = await runs_store.count_refine_tasks_since(session, since)
    fixes = await runs_store.count_worker_tasks_since(session, since, TaskKind.PR_FIX)
    limits = settings.limits
    return Quotas(
        max(limits.issues_per_day - workers, 0),
        max(limits.refines_per_day - refines, 0),
        max(limits.conflict_fixes_per_day - fixes, 0),
    )


async def load_rules(session: AsyncSession) -> dict[Role, str]:
    common = await rules_store.active_rules(session, COMMON_RULES) or default_rules(COMMON_RULES)
    rules: dict[Role, str] = {}
    for role in Role:
        own = await rules_store.active_rules(session, role.value) or default_rules(role.value)
        rules[role] = compose_rules(common, own)
    return rules


async def collect_repo_docs(
    deps: PipelineDeps, gw: Gateway, run_id: int, backends: Mapping[str, AgentBackend]
) -> dict[str, str]:
    clone = task_paths(deps.config.tasks_dir, selector_key(run_id)).controller_repo
    try:
        await gw.clone(clone)
        sources = await asyncio.to_thread(read_repo_docs, clone)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, clone, ignore_errors=True)
        raise
    return {
        name: render_repo_docs(
            select_docs(sources.candidates, sources.texts, backend.auto_loaded_docs())
        )
        for name, backend in backends.items()
    }
