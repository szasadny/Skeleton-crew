import asyncio
import base64
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub, Response
from githubkit.exception import RequestFailed
from githubkit_schemas.latest.types import AppPermissionsType

from skeleton_crew.core.logging import redact
from skeleton_crew.domain.branch_policy import check_push_target, issue_number_from_branch
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.label_policy import LabelChange, check_label_change
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.domain.text_policy import truncate_text
from skeleton_crew.domain.trust_policy import ContentEdit, IssueComment, LabelEvent
from skeleton_crew.github.write_guard import (
    AuditSink,
    check_issue_number,
    check_new_issue,
    check_open_pr,
    check_push,
    check_sha,
    guarded_write,
    label_payload,
    short,
)

REVIEW_CHECK_NAME = "skeleton-crew/review"
GITHUB_ACTIONS_SLUG = "github-actions"
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
GIT_TIMEOUT_S = 900.0
_PER_PAGE = 100
_AUTH_HEADER_KEY = "http.https://github.com/.extraheader"
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
LOGS_UNAVAILABLE = "(logs unavailable)"
_EDITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      userContentEdits(first: 100, after: $cursor) {
        nodes { editedAt editor { login } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class IssueRef:
    number: int
    title: str
    body: str
    labels: frozenset[str]
    author: str
    url: str


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
    url: str


class CiState(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    NONE = "none"


@dataclass(frozen=True)
class FailedJob:
    name: str
    job_id: int | None
    details_url: str


@dataclass(frozen=True)
class CiStatus:
    state: CiState
    failed_jobs: list[FailedJob]


@dataclass(frozen=True)
class BranchRule:
    type: str
    ruleset_id: int | None
    parameters: Mapping[str, object]


class GitCommandError(Exception):
    pass


class RulesUnavailable(Exception):
    pass


# Read-only contents: GitHub then rejects any attempt to land a PR with the API token.
API_TOKEN_PERMISSIONS: AppPermissionsType = {
    "contents": "read",
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "actions": "read",
    "statuses": "read",
    "metadata": "read",
}


def github_remote_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


class Gateway(Protocol):
    @property
    def labels(self) -> LabelNames: ...
    @property
    def base_branch(self) -> str: ...
    @property
    def dry_run(self) -> bool: ...

    async def list_open_issues(self, label: str) -> list[IssueRef]: ...
    async def trust_data(self, issue_number: int) -> TrustData: ...
    async def permission_of(self, login: str) -> str: ...
    async def open_skeleton_pr_for(self, issue_number: int) -> PullRef | None: ...
    async def ci_status(self, sha: str) -> CiStatus: ...
    async def failed_job_logs(self, job_ids: Sequence[int], max_chars: int) -> str: ...
    async def branch_rules(self, branch: str) -> list[BranchRule]: ...
    async def ruleset_bypass(self, ruleset_id: int) -> str: ...
    async def installation_permissions(self) -> dict[str, str]: ...
    async def change_labels(self, change: LabelChange) -> None: ...
    async def comment(self, issue_number: int, body: str) -> None: ...
    async def create_hitl_issue(self, title: str, body: str) -> int | None: ...
    async def open_pr(self, *, branch: str, title: str, body: str) -> PullRef | None: ...
    async def set_review_check(self, *, sha: str, success: bool, summary: str) -> None: ...
    async def commits_behind(self, sha: str) -> int: ...
    async def clone(self, dest: Path) -> str: ...
    async def fetch_branch(self, repo_dir: Path, branch: str) -> str: ...
    async def push_branch(
        self, repo_dir: Path, branch: str, sha: str, expected_sha: str | None = None
    ) -> None: ...


def _git_env(token: str | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        # Env-only config: never on argv (visible in ps) and never written to .git/config.
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": _AUTH_HEADER_KEY,
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }
    return env


async def run_git(
    args: Sequence[str], *, cwd: Path | None, token: str | None, timeout_s: float = GIT_TIMEOUT_S
) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        *args,
        cwd=cwd,
        env=_git_env(token),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitCommandError(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        detail = redact(stderr.decode(errors="replace"), [token] if token else [])
        raise GitCommandError(f"git {args[0]} failed: {detail[-500:]}")
    return stdout.decode(errors="replace")


def _check_clone_config(dest: Path, token: str | None) -> None:
    config = (dest / ".git" / "config").read_text(errors="replace")
    lowered = config.casefold()
    if "extraheader" in lowered or "authorization" in lowered or (token and token in config):
        shutil.rmtree(dest, ignore_errors=True)
        raise PolicyViolation("clone config contains credentials")


async def git_clone(url: str, dest: Path, branch: str, token: str | None) -> str:
    await run_git(
        [
            "clone",
            "--quiet",
            "--no-tags",
            "--single-branch",
            "--branch",
            branch,
            "--",
            url,
            str(dest),
        ],
        cwd=None,
        token=token,
    )
    _check_clone_config(dest, token)
    return (await run_git(["rev-parse", "HEAD"], cwd=dest, token=None)).strip()


async def git_fetch(repo_dir: Path, url: str, branch: str, token: str | None) -> str:
    await run_git(
        ["fetch", "--quiet", "--no-tags", "--", url, f"refs/heads/{branch}"],
        cwd=repo_dir,
        token=token,
    )
    return (await run_git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir, token=None)).strip()


async def git_push(
    repo_dir: Path,
    url: str,
    sha: str,
    branch: str,
    token: str | None,
    expected_sha: str | None = None,
) -> None:
    ref = f"refs/heads/{branch}"
    # The lease makes GitHub refuse the push if anyone else moved the branch meanwhile.
    force = "--force" if expected_sha is None else f"--force-with-lease={ref}:{expected_sha}"
    await run_git(
        ["push", "--quiet", force, "--no-verify", url, f"{sha}:{ref}"],
        cwd=repo_dir,
        token=token,
    )


def _json_list(resp: Response[Any, Any]) -> list[Any]:
    data = resp.json()
    if not isinstance(data, list):
        raise TypeError("expected a JSON list")
    return data


def _key_list(key: str) -> Callable[[Response[Any, Any]], list[Any]]:
    def extract(resp: Response[Any, Any]) -> list[Any]:
        data = resp.json().get(key)
        if not isinstance(data, list):
            raise TypeError(f"expected a JSON list in {key!r}")
        return data

    return extract


def _login(user: Any) -> str:
    if isinstance(user, Mapping) and isinstance(user.get("login"), str):
        return str(user["login"])
    return ""


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _tail(text: str, chars: int) -> str:
    return text[len(text) - chars :] if chars > 0 else ""


def format_job_logs(logs: Sequence[tuple[int, str]], max_chars: int) -> str:
    if not logs or max_chars <= 0:
        return ""
    room = max_chars // len(logs) - 1
    sections = []
    for job_id, text in logs:
        header = f"== job {job_id} ==\n"
        sections.append((header + _tail(text, room - len(header)))[: max(room, 0)])
    return "\n".join(sections)


def _issue_ref(raw: Mapping[str, Any]) -> IssueRef:
    labels = frozenset(
        str(label["name"]) if isinstance(label, Mapping) else str(label)
        for label in raw.get("labels") or []
    )
    return IssueRef(
        number=int(raw["number"]),
        title=str(raw["title"]),
        body=str(raw.get("body") or ""),
        labels=labels,
        author=_login(raw.get("user")),
        url=str(raw["html_url"]),
    )


class GitHubGateway:
    def __init__(
        self,
        *,
        client: GitHub[Any],
        repo: str,
        labels: LabelNames,
        base_branch: str,
        dry_run: bool,
        audit: AuditSink,
        max_comment_chars: int,
        token_provider: Callable[[], Awaitable[str]],
        remote_url: Callable[[str], str] = github_remote_url,
    ) -> None:
        self._client = client
        self._repo = repo
        self._owner, self._name = repo.split("/", 1)
        self._labels = labels
        self._base_branch = base_branch
        self._dry_run = dry_run
        self._audit = audit
        self._max_comment_chars = max_comment_chars
        self._token_provider = token_provider
        self._remote_url = remote_url
        self._permissions: dict[str, str] = {}

    @property
    def labels(self) -> LabelNames:
        return self._labels

    @property
    def base_branch(self) -> str:
        return self._base_branch

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    async def list_open_issues(self, label: str) -> list[IssueRef]:
        rest = self._client.rest
        pages = rest.paginate(
            rest.issues.async_list_for_repo,
            _json_list,
            self._owner,
            self._name,
            state="open",
            labels=label,
            per_page=_PER_PAGE,
        )
        return [_issue_ref(raw) async for raw in pages if raw.get("pull_request") is None]

    async def trust_data(self, issue_number: int) -> TrustData:
        rest = self._client.rest
        label_events: list[LabelEvent] = []
        edits: list[ContentEdit] = []
        timeline = rest.paginate(
            rest.issues.async_list_events_for_timeline,
            _json_list,
            self._owner,
            self._name,
            issue_number,
            per_page=_PER_PAGE,
        )
        async for event in timeline:
            kind = event.get("event")
            if kind in ("labeled", "unlabeled"):
                label_events.append(
                    LabelEvent(
                        label=str(event["label"]["name"]),
                        actor=_login(event.get("actor")),
                        at=_ts(event["created_at"]),
                        added=kind == "labeled",
                    )
                )
            elif kind == "renamed":
                edits.append(ContentEdit(_login(event.get("actor")), _ts(event["created_at"])))
        edits.extend(await self._body_edits(issue_number))
        comments = rest.paginate(
            rest.issues.async_list_comments,
            _json_list,
            self._owner,
            self._name,
            issue_number,
            per_page=_PER_PAGE,
        )
        issue_comments = [
            IssueComment(_login(c.get("user")), str(c.get("body") or ""), _ts(c["created_at"]))
            async for c in comments
        ]
        return TrustData(label_events, edits, issue_comments)

    async def _body_edits(self, issue_number: int) -> list[ContentEdit]:
        edits: list[ContentEdit] = []
        cursor: str | None = None
        while True:
            data = await self._client.async_graphql(
                _EDITS_QUERY,
                {
                    "owner": self._owner,
                    "name": self._name,
                    "number": issue_number,
                    "cursor": cursor,
                },
            )
            issue = data["repository"]["issue"]
            if issue is None:
                raise LookupError(f"issue #{issue_number} not found")
            page = issue["userContentEdits"]
            for node in page["nodes"]:
                edits.append(ContentEdit(_login(node.get("editor")), _ts(node["editedAt"])))
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

    async def open_skeleton_pr_for(self, issue_number: int) -> PullRef | None:
        rest = self._client.rest
        pulls = rest.paginate(
            rest.pulls.async_list,
            _json_list,
            self._owner,
            self._name,
            state="open",
            per_page=_PER_PAGE,
        )
        async for pr in pulls:
            head_repo = (pr.get("head") or {}).get("repo") or {}
            base_repo = (pr.get("base") or {}).get("repo") or {}
            same_repo = head_repo.get("id") is not None and head_repo.get("id") == base_repo.get(
                "id"
            )
            if same_repo and issue_number_from_branch(pr["head"]["ref"]) == issue_number:
                return PullRef(
                    number=int(pr["number"]),
                    head_ref=str(pr["head"]["ref"]),
                    head_sha=str(pr["head"]["sha"]),
                    url=str(pr["html_url"]),
                )
        return None

    async def ci_status(self, sha: str) -> CiStatus:
        check_sha(sha)
        rest = self._client.rest
        runs = rest.paginate(
            rest.checks.async_list_for_ref,
            _key_list("check_runs"),
            self._owner,
            self._name,
            sha,
            per_page=_PER_PAGE,
        )
        statuses = rest.paginate(
            rest.repos.async_get_combined_status_for_ref,
            _key_list("statuses"),
            self._owner,
            self._name,
            sha,
            per_page=_PER_PAGE,
        )
        return evaluate_ci([r async for r in runs], [s async for s in statuses])

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

    async def branch_rules(self, branch: str) -> list[BranchRule]:
        rest = self._client.rest
        rules = rest.paginate(
            rest.repos.async_get_branch_rules,
            _json_list,
            self._owner,
            self._name,
            branch,
            per_page=_PER_PAGE,
        )
        try:
            return [
                BranchRule(
                    type=str(rule.get("type") or ""),
                    ruleset_id=rule.get("ruleset_id"),
                    parameters=rule.get("parameters") or {},
                )
                async for rule in rules
            ]
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
                    if exc.response.status_code != 404:
                        raise

        await guarded_write(
            self._audit,
            dry_run=self._dry_run,
            action="change_labels",
            payload=label_payload(change),
            check=lambda: check_label_change(change, self._labels),
            perform=perform,
            dry_result=None,
        )

    async def comment(self, issue_number: int, body: str) -> None:
        capped = truncate_text(body, self._max_comment_chars)

        async def perform() -> None:
            await self._client.rest.issues.async_create_comment(
                self._owner, self._name, issue_number, body=capped
            )

        await guarded_write(
            self._audit,
            dry_run=self._dry_run,
            action="comment",
            payload={"issue": issue_number, "body": short(capped)},
            check=lambda: check_issue_number(issue_number),
            perform=perform,
            dry_result=None,
        )

    async def create_hitl_issue(self, title: str, body: str) -> int | None:
        capped = truncate_text(body, self._max_comment_chars)
        labels = frozenset({self._labels.hitl})

        def check() -> None:
            check_new_issue(title, self._labels)

        async def perform() -> int | None:
            resp = await self._client.rest.issues.async_create(
                self._owner, self._name, title=title, body=capped, labels=sorted(labels)
            )
            return int(resp.json()["number"])

        return await guarded_write(
            self._audit,
            dry_run=self._dry_run,
            action="create_issue",
            payload={"title": short(title), "body": short(capped), "labels": sorted(labels)},
            check=check,
            perform=perform,
            dry_result=None,
        )

    async def open_pr(self, *, branch: str, title: str, body: str) -> PullRef | None:
        async def perform() -> PullRef | None:
            resp = await self._client.rest.pulls.async_create(
                self._owner,
                self._name,
                title=title,
                head=branch,
                base=self._base_branch,
                body=body,
                draft=False,
            )
            pr = resp.json()
            return PullRef(
                number=int(pr["number"]),
                head_ref=str(pr["head"]["ref"]),
                head_sha=str(pr["head"]["sha"]),
                url=str(pr["html_url"]),
            )

        return await guarded_write(
            self._audit,
            dry_run=self._dry_run,
            action="open_pr",
            payload={"branch": short(branch), "title": short(title), "body": short(body)},
            check=lambda: check_open_pr(branch, title, self._base_branch),
            perform=perform,
            dry_result=None,
        )

    async def set_review_check(self, *, sha: str, success: bool, summary: str) -> None:
        capped = truncate_text(summary, self._max_comment_chars)

        async def perform() -> None:
            await self._client.rest.checks.async_create(
                self._owner,
                self._name,
                name=REVIEW_CHECK_NAME,
                head_sha=sha,
                status="completed",
                conclusion="success" if success else "failure",
                output={
                    "title": "Review passed" if success else "Changes requested",
                    "summary": capped,
                },
            )

        await guarded_write(
            self._audit,
            dry_run=self._dry_run,
            action="set_review_check",
            payload={"sha": short(sha), "success": success, "summary": short(capped)},
            check=lambda: check_sha(sha),
            perform=perform,
            dry_result=None,
        )

    async def commits_behind(self, sha: str) -> int:
        check_sha(sha)
        resp = await self._client.rest.repos.async_compare_commits(
            self._owner, self._name, f"{self._base_branch}...{sha}", per_page=1
        )
        return int(resp.json()["behind_by"])

    async def clone(self, dest: Path) -> str:
        token = await self._token_provider()
        return await git_clone(self._remote_url(self._repo), dest, self._base_branch, token)

    async def fetch_branch(self, repo_dir: Path, branch: str) -> str:
        check_push_target(branch, self._base_branch)
        token = await self._token_provider()
        return await git_fetch(repo_dir, self._remote_url(self._repo), branch, token)

    async def push_branch(
        self, repo_dir: Path, branch: str, sha: str, expected_sha: str | None = None
    ) -> None:
        async def perform() -> None:
            token = await self._token_provider()
            url = self._remote_url(self._repo)
            await git_push(repo_dir, url, sha, branch, token, expected_sha)

        def check() -> None:
            check_push(branch, sha, self._base_branch)
            if expected_sha is not None:
                check_sha(expected_sha)

        payload = {"branch": short(branch), "sha": short(sha)}
        await guarded_write(
            self._audit,
            dry_run=self._dry_run,
            action="push_branch",
            payload=payload if expected_sha is None else {**payload, "expected": expected_sha},
            check=check,
            perform=perform,
            dry_result=None,
        )


def _run_failed(run: Mapping[str, Any]) -> FailedJob:
    app = run.get("app") or {}
    is_actions = isinstance(app, Mapping) and app.get("slug") == GITHUB_ACTIONS_SLUG
    return FailedJob(
        name=str(run.get("name") or ""),
        job_id=int(run["id"]) if is_actions and run.get("id") is not None else None,
        details_url=str(run.get("details_url") or run.get("html_url") or ""),
    )


def evaluate_ci(
    check_runs: Sequence[Mapping[str, Any]], statuses: Sequence[Mapping[str, Any]]
) -> CiStatus:
    reported = pending = False
    failed: list[FailedJob] = []
    for run in check_runs:
        if run.get("name") == REVIEW_CHECK_NAME:
            continue
        reported = True
        if run.get("status") != "completed":
            pending = True
        elif run.get("conclusion") not in _GREEN_CONCLUSIONS:
            failed.append(_run_failed(run))
    for status in statuses:
        reported = True
        state = status.get("state")
        if state == "pending":
            pending = True
        elif state != "success":
            failed.append(
                FailedJob(
                    str(status.get("context") or ""), None, str(status.get("target_url") or "")
                )
            )
    if not reported:
        return CiStatus(CiState.NONE, [])
    if pending:
        return CiStatus(CiState.PENDING, failed)
    if failed:
        return CiStatus(CiState.FAILURE, failed)
    return CiStatus(CiState.SUCCESS, [])


class RepoTokenProvider:
    def __init__(
        self,
        app_client: GitHub[Any],
        installation_id: int,
        repo_name: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._app_client = app_client
        self._installation_id = installation_id
        self._repo_name = repo_name
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=UTC)

    async def __call__(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - TOKEN_REFRESH_MARGIN:
                resp = await self._app_client.rest.apps.async_create_installation_access_token(
                    self._installation_id,
                    repositories=[self._repo_name],
                    permissions={"contents": "write"},
                )
                data = resp.json()
                self._token = str(data["token"])
                self._expires_at = _ts(data["expires_at"])
            return self._token


async def make_github_client(
    app_id: int, private_key: str, installation_id: int | None, repo: str
) -> tuple[GitHub[Any], Callable[[], Awaitable[str]]]:
    owner, name = repo.split("/", 1)
    app_client = GitHub(AppAuthStrategy(app_id, private_key))
    if installation_id is None:
        resp = await app_client.rest.apps.async_get_repo_installation(owner, name)
        installation_id = int(resp.json()["id"])
    client = GitHub(
        AppInstallationAuthStrategy(
            app_id,
            private_key,
            installation_id,
            repositories=[name],
            permissions=API_TOKEN_PERMISSIONS,
        )
    )
    return client, RepoTokenProvider(app_client, installation_id, name)
