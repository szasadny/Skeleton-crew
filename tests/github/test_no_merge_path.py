import re
from pathlib import Path

import pytest

import skeleton_crew.github

GITHUB_SRC = Path(skeleton_crew.github.__file__).parent
FORBIDDEN = [
    r"merge",
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
    assert {"gateway.py", "trust.py", "preflight.py", "write_guard.py"} <= names


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


def test_gateway_exposes_no_merge_like_method() -> None:
    from skeleton_crew.github.gateway import Gateway, GitHubGateway

    allowed = {
        "list_open_issues",
        "trust_data",
        "permission_of",
        "open_skeleton_pr_for",
        "ci_status",
        "commits_behind",
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
