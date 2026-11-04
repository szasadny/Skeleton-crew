from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.branch_policy import check_push_target, issue_number_from_branch
from skeleton_crew.domain.label_policy import LabelChange, check_label_change
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.domain.text_policy import truncate_text
from skeleton_crew.domain.trust_policy import ContentEdit, IssueComment, LabelEvent
from skeleton_crew.github.gateway import (
    BranchRule,
    CiState,
    CiStatus,
    IssueRef,
    PullRef,
    TrustData,
    format_job_logs,
    git_branch_head,
    git_clone,
    git_fetch,
    git_push,
)
from skeleton_crew.github.preflight import REQUIRED_PERMISSIONS
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

BOT_LOGIN = "skeleton-crew[bot]"
MAINTAINER = "maintainer"
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class FakeIssue:
    number: int
    title: str
    body: str = ""
    labels: set[str] = field(default_factory=set)
    author: str = "author"
    label_events: list[LabelEvent] = field(default_factory=list)
    edits: list[ContentEdit] = field(default_factory=list)
    comments: list[IssueComment] = field(default_factory=list)

    def ref(self) -> IssueRef:
        return IssueRef(
            number=self.number,
            title=self.title,
            body=self.body,
            labels=frozenset(self.labels),
            author=self.author,
            url=f"https://github.com/o/r/issues/{self.number}",
        )


@dataclass
class FakePull:
    number: int
    head_ref: str
    head_sha: str
    same_repo: bool = True

    def ref(self) -> PullRef:
        return PullRef(
            self.number, self.head_ref, self.head_sha, f"https://github.com/o/r/pull/{self.number}"
        )


@dataclass(frozen=True)
class AuditRecord:
    kind: AuditKind
    outcome: AuditOutcome
    summary: str
    payload: Mapping[str, object] | None


@dataclass(frozen=True)
class WriteRecord:
    action: str
    args: Mapping[str, object]


class FakeGitHubGateway:
    def __init__(
        self,
        *,
        labels: LabelNames | None = None,
        base_branch: str = "main",
        dry_run: bool = False,
        max_comment_chars: int = 1500,
        audit: AuditSink | None = None,
        source_repo: Path | None = None,
        push_remote: Path | None = None,
    ) -> None:
        self.labels = labels or LabelNames()
        self.base_branch = base_branch
        self.dry_run = dry_run
        self.max_comment_chars = max_comment_chars
        self.source_repo = source_repo
        self.push_remote = push_remote
        self._external_audit = audit
        self.issues: dict[int, FakeIssue] = {}
        self.pulls: list[FakePull] = []
        self.ci: dict[str, list[CiStatus]] = {}
        self.ci_required: list[frozenset[str]] = []
        self.job_logs: dict[int, str] = {}
        self.permissions: dict[str, str] = {MAINTAINER: "maintain"}
        self.rules: list[BranchRule] = [
            BranchRule("pull_request", 1, {"required_approving_review_count": 1})
        ]
        self.ruleset_bypasses: dict[int, str] = {1: "never"}
        self.installation_perms: dict[str, str] = dict(REQUIRED_PERMISSIONS)
        self.failures: dict[str, Exception] = {}
        self.writes: list[WriteRecord] = []
        self.audit_events: list[AuditRecord] = []
        self.review_checks: dict[str, tuple[bool, str]] = {}
        self.pushes: list[tuple[str, str]] = []
        self.leases: list[str | None] = []
        self.behind: dict[str, int] = {}
        self.permission_lookups: list[str] = []
        self._ticks = 0

    def tick(self) -> datetime:
        self._ticks += 1
        return _EPOCH + timedelta(minutes=self._ticks)

    def add_issue(
        self,
        number: int,
        title: str,
        *,
        body: str = "",
        labels: Iterable[str] = (),
        author: str = "author",
        labeled_by: str = MAINTAINER,
    ) -> FakeIssue:
        issue = FakeIssue(number, title, body, set(), author)
        for label in labels:
            issue.labels.add(label)
            issue.label_events.append(LabelEvent(label, labeled_by, self.tick(), added=True))
        self.issues[number] = issue
        return issue

    def script_ci(self, sha: str, *states: CiStatus | CiState) -> None:
        self.ci[sha] = [s if isinstance(s, CiStatus) else CiStatus(s, []) for s in states]

    def _maybe_fail(self, name: str) -> None:
        if name in self.failures:
            raise self.failures[name]

    async def _audit(
        self,
        kind: AuditKind,
        outcome: AuditOutcome,
        summary: str,
        payload: Mapping[str, object] | None,
    ) -> None:
        self.audit_events.append(AuditRecord(kind, outcome, summary, payload))
        if self._external_audit is not None:
            await self._external_audit(kind, outcome, summary, payload)

    def _record(self, action: str, **args: object) -> None:
        self.writes.append(WriteRecord(action, args))

    async def list_open_issues(self, label: str) -> list[IssueRef]:
        self._maybe_fail("list_open_issues")
        key = label.casefold()
        return [
            issue.ref()
            for issue in sorted(self.issues.values(), key=lambda i: i.number)
            if any(name.casefold() == key for name in issue.labels)
        ]

    async def trust_data(self, issue_number: int) -> TrustData:
        self._maybe_fail("trust_data")
        issue = self.issues[issue_number]
        return TrustData(list(issue.label_events), list(issue.edits), list(issue.comments))

    async def permission_of(self, login: str) -> str:
        self._maybe_fail("permission_of")
        self.permission_lookups.append(login)
        return self.permissions.get(login, "read")

    async def open_skeleton_pr_for(self, issue_number: int) -> PullRef | None:
        for pull in self.pulls:
            if pull.same_repo and issue_number_from_branch(pull.head_ref) == issue_number:
                return pull.ref()
        return None

    async def ci_status(self, sha: str, required: Collection[str] = ()) -> CiStatus:
        check_sha(sha)
        self._maybe_fail("ci_status")
        self.ci_required.append(frozenset(required))
        script = self.ci.get(sha)
        if not script:
            return CiStatus(CiState.NONE, [])
        return script.pop(0) if len(script) > 1 else script[0]

    async def failed_job_logs(self, job_ids: Sequence[int], max_chars: int) -> str:
        logs = [(i, self.job_logs.get(i, "(logs unavailable)")) for i in job_ids if i > 0]
        return format_job_logs(logs, max_chars)

    async def branch_rules(self, branch: str) -> list[BranchRule]:
        self._maybe_fail("branch_rules")
        return list(self.rules)

    async def ruleset_bypass(self, ruleset_id: int) -> str:
        self._maybe_fail("ruleset_bypass")
        return self.ruleset_bypasses.get(ruleset_id, "")

    async def installation_permissions(self) -> dict[str, str]:
        self._maybe_fail("installation_permissions")
        return dict(self.installation_perms)

    async def change_labels(self, change: LabelChange) -> None:
        async def perform() -> None:
            issue = self.issues[change.issue_number]
            removed = {name.casefold() for name in change.remove}
            for name in sorted(change.add):
                issue.labels.add(name)
                issue.label_events.append(LabelEvent(name, BOT_LOGIN, self.tick(), added=True))
            for name in [n for n in issue.labels if n.casefold() in removed]:
                issue.labels.discard(name)
                issue.label_events.append(LabelEvent(name, BOT_LOGIN, self.tick(), added=False))
            self._record("change_labels", **label_payload(change))

        await guarded_write(
            self._audit,
            dry_run=self.dry_run,
            action="change_labels",
            payload=label_payload(change),
            check=lambda: check_label_change(change, self.labels),
            perform=perform,
            dry_result=None,
        )

    async def comment(self, issue_number: int, body: str) -> None:
        capped = truncate_text(body, self.max_comment_chars)

        async def perform() -> None:
            self.issues[issue_number].comments.append(IssueComment(BOT_LOGIN, capped, self.tick()))
            self._record("comment", issue=issue_number, body=capped)

        await guarded_write(
            self._audit,
            dry_run=self.dry_run,
            action="comment",
            payload={"issue": issue_number, "body": short(capped)},
            check=lambda: check_issue_number(issue_number),
            perform=perform,
            dry_result=None,
        )

    async def create_hitl_issue(self, title: str, body: str) -> int | None:
        capped = truncate_text(body, self.max_comment_chars)
        labels = frozenset({self.labels.hitl})

        def check() -> None:
            check_new_issue(title, self.labels)

        async def perform() -> int | None:
            number = max([*self.issues, *(p.number for p in self.pulls), 0]) + 1
            self.add_issue(number, title, body=capped, labels=labels, labeled_by=BOT_LOGIN)
            self._record("create_issue", number=number, title=title, body=capped)
            return number

        return await guarded_write(
            self._audit,
            dry_run=self.dry_run,
            action="create_issue",
            payload={"title": short(title), "body": short(capped), "labels": sorted(labels)},
            check=check,
            perform=perform,
            dry_result=None,
        )

    async def open_pr(self, *, branch: str, title: str, body: str) -> PullRef | None:
        async def perform() -> PullRef | None:
            number = max([*self.issues, *(p.number for p in self.pulls), 0]) + 1
            head = next((sha for b, sha in reversed(self.pushes) if b == branch), "0" * 40)
            pull = FakePull(number, branch, head)
            self.pulls.append(pull)
            self._record("open_pr", branch=branch, title=title, body=body)
            return pull.ref()

        return await guarded_write(
            self._audit,
            dry_run=self.dry_run,
            action="open_pr",
            payload={"branch": short(branch), "title": short(title), "body": short(body)},
            check=lambda: check_open_pr(branch, title, self.base_branch),
            perform=perform,
            dry_result=None,
        )

    async def set_review_check(self, *, sha: str, success: bool, summary: str) -> None:
        capped = truncate_text(summary, self.max_comment_chars)

        async def perform() -> None:
            self.review_checks[sha] = (success, capped)
            self._record("set_review_check", sha=sha, success=success, summary=capped)

        await guarded_write(
            self._audit,
            dry_run=self.dry_run,
            action="set_review_check",
            payload={"sha": short(sha), "success": success, "summary": short(capped)},
            check=lambda: check_sha(sha),
            perform=perform,
            dry_result=None,
        )

    async def commits_behind(self, sha: str) -> int:
        check_sha(sha)
        self._maybe_fail("commits_behind")
        return self.behind.get(sha, 0)

    async def clone(self, dest: Path) -> str:
        if self.source_repo is None:
            raise RuntimeError("FakeGitHubGateway.clone needs source_repo")
        return await git_clone(str(self.source_repo), dest, self.base_branch, None)

    async def fetch_branch(self, repo_dir: Path, branch: str) -> str:
        check_push_target(branch, self.base_branch)
        if self.push_remote is None:
            raise RuntimeError("FakeGitHubGateway.fetch_branch needs push_remote")
        return await git_fetch(repo_dir, str(self.push_remote), branch, None)

    async def branch_head(self, branch: str) -> str | None:
        check_push_target(branch, self.base_branch)
        self._maybe_fail("branch_head")
        if self.push_remote is None:
            return None
        return await git_branch_head(str(self.push_remote), branch, None)

    async def push_branch(
        self, repo_dir: Path, branch: str, sha: str, expected_sha: str | None = None
    ) -> None:
        async def perform() -> None:
            self._maybe_fail("push_branch")
            if self.push_remote is not None:
                await git_push(repo_dir, str(self.push_remote), sha, branch, None, expected_sha)
            self.pushes.append((branch, sha))
            self.leases.append(expected_sha)
            self._record("push_branch", branch=branch, sha=sha)

        await guarded_write(
            self._audit,
            dry_run=self.dry_run,
            action="push_branch",
            payload={"branch": short(branch), "sha": short(sha)},
            check=lambda: check_push(branch, sha, self.base_branch),
            perform=perform,
            dry_result=None,
        )
