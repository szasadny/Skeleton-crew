import inspect
import re
from pathlib import Path

import pytest

import skeleton_crew.github

GITHUB_SRC = Path(skeleton_crew.github.__file__).parent
# Read-only words that contain "merge": the mergeable flag, merged_at and closed-unmerged lookups.
MERGE = r"(?<!un)merge(?!able\b|d_at\b)"
FORBIDDEN = [
    MERGE,
    r"create_review",
    r"submit_review",
    r"approve",
    r"auto_merge|automerge",
    r"mutation",
    r"pulls\.(async_)?update",
    r"issues\.(async_)?update",
    r"[\"']state[\"']\s*:\s*[\"']closed",
    r"state\s*=\s*[\"']closed",
    r"git/refs",
    r"delete_ref",
    r"delete_branch",
    r"--delete",
    r"--mirror",
    r"[\"'](PUT|PATCH|DELETE)[\"']",
    r"\.arequest\(",
    r"\.request\(",
]


def _sources() -> list[Path]:
    return sorted(GITHUB_SRC.glob("*.py"))


def test_github_package_sources_found() -> None:
    names = {p.name for p in _sources()}
    assert {
        "gateway.py",
        "_git.py",
        "_ci.py",
        "trust.py",
        "preflight.py",
        "write_guard.py",
    } <= names


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_github_package_has_no_forbidden_operation(pattern: str) -> None:
    regex = re.compile(pattern, re.IGNORECASE)
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for path in _sources()
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if regex.search(line)
    ]
    assert hits == []


@pytest.mark.parametrize(
    "line",
    [
        "rest.pulls.async_merge(owner, name, number)",
        'merge_method="squash"',
        "enablePullRequestAutoMerge",
        "def merge_pr(self) -> None:",
        "unmerge_and_merge()",
        "mergeable_merge()",
    ],
)
def test_merge_pattern_catches_merge_calls(line: str) -> None:
    assert re.search(MERGE, line, re.IGNORECASE)


@pytest.mark.parametrize(
    "line",
    [
        "async def pr_mergeable(self, number: int) -> bool | None:",
        'value = resp.json().get("mergeable")',
        'if pr.get("merged_at") is None:',
        "async def closed_unmerged_pr_for(self, branch: str) -> PullRef | None:",
    ],
)
def test_merge_pattern_allows_read_only_words(line: str) -> None:
    assert not re.search(MERGE, line, re.IGNORECASE)


def test_gateway_exposes_no_merge_like_method() -> None:
    from skeleton_crew.github.gateway import Gateway, GitHubGateway

    allowed = {
        "list_open_issues",
        "trust_data",
        "permission_of",
        "open_skeleton_prs",
        "closed_unmerged_pr_for",
        "pr_mergeable",
        "ci_status",
        "fetch_branch",
        "branch_head",
        "failed_job_logs",
        "branch_rules",
        "ruleset_bypass",
        "installation_permissions",
        "change_labels",
        "comment",
        "create_hitl_issue",
        "open_pr",
        "set_review_check",
        "clone",
        "push_branch",
        "labels",
        "base_branch",
        "dry_run",
    }
    for cls in (Gateway, GitHubGateway):
        public = {name for name in vars(cls) if not name.startswith("_")}
        assert public <= allowed


def test_private_gateway_modules_expose_no_merge_like_function() -> None:
    from skeleton_crew.github import _ci, _git

    allowed = {
        _git: {"auth_env", "git_clone", "git_fetch", "git_branch_head", "git_push"},
        _ci: {"evaluate_ci", "required_checks", "format_job_logs"},
    }
    for module, names in allowed.items():
        functions = {
            name
            for name, value in vars(module).items()
            if inspect.isfunction(value)
            and value.__module__ == module.__name__
            and not name.startswith("_")
        }
        assert functions == names
