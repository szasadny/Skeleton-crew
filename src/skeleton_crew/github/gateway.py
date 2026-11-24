from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal, Protocol, Self
from urllib.parse import quote

from githubkit import GitHub
from githubkit.exception import RequestFailed

from skeleton_crew.core.audit import AuditSink
from skeleton_crew.core.config import GITHUB_URL
from skeleton_crew.domain.branch_policy import (
    check_push_target,
    is_skeleton_branch,
    issue_number_from_branch,
)
from skeleton_crew.domain.ci_policy import (
    REVIEW_CHECK_NAME,
    BranchRule,
    CiStatus,
    ReviewCheck,
    evaluate_ci,
    format_job_logs,
)
from skeleton_crew.domain.label_policy import LabelChange
from skeleton_crew.domain.settings import AppSettings, LabelNames
from skeleton_crew.domain.text_policy import truncate_text
from skeleton_crew.domain.trust_policy import ContentEdit, IssueComment, LabelEvent
from skeleton_crew.domain.write_policy import check_sha, new_issue_labels
from skeleton_crew.github._auth import make_github_client
from skeleton_crew.github._git import (
    REMOTE_PROTOCOL,
    GitRemote,
    git_branch_head,
    git_clone,
    git_fetch,
    git_push,
)
from skeleton_crew.github._parse import (
    EDITS_QUERY,
    PER_PAGE,
    Extract,
    body_edit,
    branch_rule,
    issue_comment,
    json_list,
    key_list,
    label_event,
    rename_edit,
)
from skeleton_crew.github.write_guard import (
    WriteGuard,
    comment_spec,
    label_spec,
    new_issue_spec,
    open_pr_spec,
    push_spec,
    review_check_spec,
)

_ACTIVE_RUN_STATUSES: tuple[Literal["queued", "in_progress"], ...] = ("queued", "in_progress")
LOGS_UNAVAILABLE = "(logs unavailable)"
_REVIEW_CHECK_RUNS: dict[ReviewCheck, tuple[str, str | None, str]] = {
    ReviewCheck.PENDING: ("in_progress", None, "Waiting for review"),
    ReviewCheck.PASSED: ("completed", "success", "Review passed"),
    ReviewCheck.CHANGES_REQUESTED: ("completed", "failure", "Changes requested"),
}

type RemoteFactory = Callable[[], Awaitable[GitRemote]]


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    body: str
    labels: frozenset[str]

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        labels = frozenset(
            str(label["name"]) if isinstance(label, Mapping) else str(label)
            for label in raw.get("labels") or []
        )
        return cls(int(raw["number"]), str(raw["title"]), str(raw.get("body") or ""), labels)


@dataclass(frozen=True)
class TrustData:
    label_events: list[LabelEvent]
    edits: list[ContentEdit]
    comments: list[IssueComment]


@dataclass(frozen=True)
class PullRef:
    number: int
    head_ref: str
    head_sha: str

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Self:
        return cls(int(raw["number"]), str(raw["head"]["ref"]), str(raw["head"]["sha"]))


@dataclass(frozen=True)
class GatewayConfig:
    repo: str
    labels: LabelNames
    base_branch: str
    dry_run: bool
    max_comment_chars: int

    @classmethod
    def from_settings(cls, repo: str, settings: AppSettings) -> Self:
        return cls(
            repo=repo,
            labels=settings.labels,
            base_branch=settings.repo.base_branch,
            dry_run=settings.dry_run,
            max_comment_chars=settings.limits.max_comment_chars,
        )


class RulesUnavailable(Exception):
    pass


def github_remote_url(repo: str) -> str:
    return f"{GITHUB_URL}/{repo}.git"


async def connect_github(
    app_id: int, private_key: str, installation_id: int | None, repo: str
) -> tuple[GitHub[Any], RemoteFactory]:
    client, token_provider = await make_github_client(app_id, private_key, installation_id, repo)
    url = github_remote_url(repo)

    async def remote() -> GitRemote:
        return GitRemote(url, REMOTE_PROTOCOL, await token_provider())

    return client, remote


class Gateway(Protocol):
    @property
    def labels(self) -> LabelNames: ...
    @property
    def dry_run(self) -> bool: ...

    async def list_open_issues(self, label: str) -> list[IssueRef]: ...
    async def trust_data(self, issue_number: int) -> TrustData: ...
    async def permission_of(self, login: str) -> str: ...
    async def open_skeleton_prs(self) -> dict[int, tuple[PullRef, ...]]: ...
    async def closed_unmerged_pr_for(self, branch: str) -> PullRef | None: ...
    async def pr_mergeable(self, number: int) -> bool | None: ...
    async def ci_status(self, sha: str, required: Collection[str] = ()) -> CiStatus: ...
    async def failed_job_logs(self, job_ids: Sequence[int], max_chars: int) -> str: ...
    async def skeleton_ci_active(self) -> bool: ...
    async def branch_rules(self, branch: str) -> list[BranchRule]: ...
    async def ruleset_bypass(self, ruleset_id: int) -> str: ...
    async def installation_permissions(self) -> dict[str, str]: ...
    async def repo_is_private(self) -> bool: ...
    async def change_labels(self, change: LabelChange) -> None: ...
    async def comment(self, issue_number: int, body: str) -> None: ...
    async def create_hitl_issue(self, title: str, body: str) -> int | None: ...
    async def open_pr(self, *, branch: str, title: str, body: str) -> PullRef | None: ...
    async def set_review_check(self, *, sha: str, state: ReviewCheck, summary: str) -> None: ...
    async def clone(self, dest: Path) -> str: ...
    async def fetch_branch(self, repo_dir: Path, branch: str) -> str: ...
    async def branch_head(self, branch: str) -> str | None: ...
    async def push_branch(
        self, repo_dir: Path, branch: str, sha: str, expected_sha: str | None = None
    ) -> None: ...


class GitHubGateway:
    def __init__(
        self,
        client: GitHub[Any],
        config: GatewayConfig,
        audit: AuditSink,
        remote_factory: RemoteFactory,
    ) -> None:
        self._client = client
        self._config = config
        self._owner, self._name = config.repo.split("/", 1)
        self._guard = WriteGuard(audit, dry_run=config.dry_run)
        self._remote = remote_factory
        self._permissions: dict[str, str] = {}

    @property
    def labels(self) -> LabelNames:
        return self._config.labels

    @property
    def dry_run(self) -> bool:
        return self._config.dry_run

    def _pages(
        self,
        request: Callable[..., Awaitable[Any]],
        *args: object,
        extract: Extract = json_list,
        **params: object,
    ) -> AsyncIterator[Any]:
        rest = self._client.rest
        return rest.paginate(
            request, extract, self._owner, self._name, *args, per_page=PER_PAGE, **params
        )

    def _capped(self, text: str) -> str:
        return truncate_text(text, self._config.max_comment_chars)

    async def list_open_issues(self, label: str) -> list[IssueRef]:
        rest = self._client.rest
        issues = self._pages(rest.issues.async_list_for_repo, state="open", labels=label)
        return [IssueRef.from_api(raw) async for raw in issues if raw.get("pull_request") is None]

    async def trust_data(self, issue_number: int) -> TrustData:
        rest = self._client.rest
        label_events: list[LabelEvent] = []
        edits: list[ContentEdit] = []
        async for event in self._pages(rest.issues.async_list_events_for_timeline, issue_number):
            kind = event.get("event")
            if kind in ("labeled", "unlabeled"):
                label_events.append(label_event(event))
            elif kind == "renamed":
                edits.append(rename_edit(event))
        edits.extend(await self._body_edits(issue_number))
        comments = self._pages(rest.issues.async_list_comments, issue_number)
        return TrustData(label_events, edits, [issue_comment(c) async for c in comments])

    async def _body_edits(self, issue_number: int) -> list[ContentEdit]:
        edits: list[ContentEdit] = []
        cursor: str | None = None
        while True:
            data = await self._client.async_graphql(
                EDITS_QUERY,
                {
                    "owner": self._owner,
                    "name": self._name,
                    "number": issue_number,
                    "cursor": cursor,
                    "first": PER_PAGE,
                },
            )
            issue = data["repository"]["issue"]
            if issue is None:
                raise LookupError(f"issue #{issue_number} not found")
            page = issue["userContentEdits"]
            edits.extend(body_edit(node) for node in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return edits
            cursor = page["pageInfo"]["endCursor"]

    async def permission_of(self, login: str) -> str:
        key = login.casefold()
        if key not in self._permissions:
            resp = await self._client.rest.repos.async_get_collaborator_permission_level(
                self._owner, self._name, login
            )
            self._permissions[key] = str(resp.json().get("role_name") or "")
        return self._permissions[key]

    async def open_skeleton_prs(self) -> dict[int, tuple[PullRef, ...]]:
        found: dict[int, tuple[PullRef, ...]] = {}
        async for pr in self._pages(self._client.rest.pulls.async_list, state="open"):
            head_repo = (pr.get("head") or {}).get("repo") or {}
            base_repo = (pr.get("base") or {}).get("repo") or {}
            same_repo = head_repo.get("id") is not None and head_repo.get("id") == base_repo.get(
                "id"
            )
            issue_number = issue_number_from_branch(pr["head"]["ref"])
            if same_repo and issue_number is not None:
                found[issue_number] = (*found.get(issue_number, ()), PullRef.from_api(pr))
        return found

    async def closed_unmerged_pr_for(self, branch: str) -> PullRef | None:
        check_push_target(branch, self._config.base_branch)
        pulls = self._pages(
            self._client.rest.pulls.async_list, state="all", head=f"{self._owner}:{branch}"
        )
        async for pr in pulls:
            ended = pr.get("state") != "open" and pr.get("merged_at") is None
            if ended and pr["head"]["ref"] == branch:
                return PullRef.from_api(pr)
        return None

    async def pr_mergeable(self, number: int) -> bool | None:
        resp = await self._client.rest.pulls.async_get(self._owner, self._name, number)
        value = resp.json().get("mergeable")
        return value if isinstance(value, bool) else None

    async def ci_status(self, sha: str, required: Collection[str] = ()) -> CiStatus:
        check_sha(sha)
        rest = self._client.rest
        runs = self._pages(rest.checks.async_list_for_ref, sha, extract=key_list("check_runs"))
        statuses = self._pages(
            rest.repos.async_get_combined_status_for_ref, sha, extract=key_list("statuses")
        )
        workflows = self._pages(
            rest.actions.async_list_workflow_runs_for_repo,
            extract=key_list("workflow_runs"),
            head_sha=sha,
        )
        return evaluate_ci(
            [r async for r in runs],
            [s async for s in statuses],
            [w async for w in workflows],
            required,
        )

    async def failed_job_logs(self, job_ids: Sequence[int], max_chars: int) -> str:
        logs: list[tuple[int, str]] = []
        for job_id in job_ids:
            if job_id < 1:
                continue
            try:
                resp = await self._client.rest.actions.async_download_job_logs_for_workflow_run(
                    self._owner, self._name, job_id
                )
                logs.append((job_id, resp.content.decode("utf-8", errors="replace")))
            except RequestFailed:
                logs.append((job_id, LOGS_UNAVAILABLE))
        return format_job_logs(logs, max_chars)

    async def skeleton_ci_active(self) -> bool:
        for status in _ACTIVE_RUN_STATUSES:
            runs = self._pages(
                self._client.rest.actions.async_list_workflow_runs_for_repo,
                extract=key_list("workflow_runs"),
                status=status,
            )
            async for run in runs:
                if is_skeleton_branch(str(run.get("head_branch") or "")):
                    return True
        return False

    async def branch_rules(self, branch: str) -> list[BranchRule]:
        rules = self._pages(self._client.rest.repos.async_get_branch_rules, branch)
        try:
            return [branch_rule(rule) async for rule in rules]
        except RequestFailed as exc:
            # GitHub answers 403 when the plan has no rulesets (free private repos).
            if exc.response.status_code == HTTPStatus.FORBIDDEN:
                raise RulesUnavailable from exc
            raise

    async def ruleset_bypass(self, ruleset_id: int) -> str:
        resp = await self._client.rest.repos.async_get_repo_ruleset(
            self._owner, self._name, ruleset_id
        )
        return str(resp.json().get("current_user_can_bypass") or "")

    async def installation_permissions(self) -> dict[str, str]:
        resp = await self._client.rest.apps.async_get_repo_installation(self._owner, self._name)
        permissions = resp.json().get("permissions") or {}
        return {str(k): str(v) for k, v in permissions.items()}

    async def repo_is_private(self) -> bool:
        resp = await self._client.rest.repos.async_get(self._owner, self._name)
        return resp.json().get("private") is True

    async def change_labels(self, change: LabelChange) -> None:
        async def perform() -> None:
            issues = self._client.rest.issues
            if change.add:
                await issues.async_add_labels(
                    self._owner, self._name, change.issue_number, labels=sorted(change.add)
                )
            for name in sorted(change.remove):
                try:
                    await issues.async_remove_label(
                        self._owner, self._name, change.issue_number, quote(name, safe="")
                    )
                except RequestFailed as exc:
                    if exc.response.status_code != HTTPStatus.NOT_FOUND:
                        raise

        await self._guard.run(label_spec(change, self._config.labels), perform)

    async def comment(self, issue_number: int, body: str) -> None:
        capped = self._capped(body)

        async def perform() -> None:
            await self._client.rest.issues.async_create_comment(
                self._owner, self._name, issue_number, body=capped
            )

        await self._guard.run(comment_spec(issue_number, capped), perform)

    async def create_hitl_issue(self, title: str, body: str) -> int | None:
        capped = self._capped(body)
        labels = self._config.labels

        async def perform() -> int:
            resp = await self._client.rest.issues.async_create(
                self._owner, self._name, title=title, body=capped, labels=new_issue_labels(labels)
            )
            return int(resp.json()["number"])

        return await self._guard.run(new_issue_spec(title, capped, labels), perform)

    async def open_pr(self, *, branch: str, title: str, body: str) -> PullRef | None:
        base_branch = self._config.base_branch

        async def perform() -> PullRef:
            resp = await self._client.rest.pulls.async_create(
                self._owner,
                self._name,
                title=title,
                head=branch,
                base=base_branch,
                body=body,
                draft=False,
            )
            return PullRef.from_api(resp.json())

        return await self._guard.run(open_pr_spec(branch, title, body, base_branch), perform)

    async def set_review_check(self, *, sha: str, state: ReviewCheck, summary: str) -> None:
        capped = self._capped(summary)

        async def perform() -> None:
            status, conclusion, title = _REVIEW_CHECK_RUNS[state]
            fields: dict[str, Any] = {"name": REVIEW_CHECK_NAME, "head_sha": sha, "status": status}
            if conclusion is not None:
                fields["conclusion"] = conclusion
            fields["output"] = {"title": title, "summary": capped}
            await self._client.rest.checks.async_create(self._owner, self._name, **fields)

        await self._guard.run(review_check_spec(sha, state, capped), perform)

    async def clone(self, dest: Path) -> str:
        return await git_clone(await self._remote(), dest, self._config.base_branch)

    async def fetch_branch(self, repo_dir: Path, branch: str) -> str:
        check_push_target(branch, self._config.base_branch)
        return await git_fetch(repo_dir, await self._remote(), branch)

    async def branch_head(self, branch: str) -> str | None:
        check_push_target(branch, self._config.base_branch)
        return await git_branch_head(await self._remote(), branch)

    async def push_branch(
        self, repo_dir: Path, branch: str, sha: str, expected_sha: str | None = None
    ) -> None:
        async def perform() -> None:
            await git_push(repo_dir, await self._remote(), sha, branch, expected_sha=expected_sha)

        spec = push_spec(branch, sha, self._config.base_branch, expected_sha)
        await self._guard.run(spec, perform)
