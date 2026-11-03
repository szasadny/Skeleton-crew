import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from githubkit import GitHub

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.settings import LabelNames
from skeleton_crew.github.gateway import GitHubGateway

API = "https://api.github.com"
REPO = "o/r"
TOKEN = "ghs_test-installation-token-0123456789"  # noqa: S105
SHA = "a" * 40

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "HOME": os.devnull,
    "PATH": os.environ.get("PATH", ""),
}


@dataclass
class AuditLog:
    events: list[tuple[AuditKind, AuditOutcome, str, Mapping[str, object] | None]] = field(
        default_factory=list
    )

    async def __call__(
        self,
        kind: AuditKind,
        outcome: AuditOutcome,
        summary: str,
        payload: Mapping[str, object] | None,
    ) -> None:
        self.events.append((kind, outcome, summary, payload))

    def kinds(self) -> list[tuple[AuditKind, AuditOutcome]]:
        return [(kind, outcome) for kind, outcome, _, _ in self.events]


def make_client() -> GitHub[Any]:
    return GitHub("test-client-token", http_cache=False, auto_retry=False)


def token_provider(calls: list[int] | None = None) -> Callable[[], Awaitable[str]]:
    async def provide() -> str:
        if calls is not None:
            calls.append(1)
        return TOKEN

    return provide


def make_gateway(
    audit: AuditLog,
    *,
    dry_run: bool = False,
    max_comment_chars: int = 1500,
    remote_url: Callable[[str], str] | None = None,
) -> GitHubGateway:
    extra = {} if remote_url is None else {"remote_url": remote_url}
    return GitHubGateway(
        client=make_client(),
        repo=REPO,
        labels=LabelNames(),
        base_branch="main",
        dry_run=dry_run,
        audit=audit,
        max_comment_chars=max_comment_chars,
        token_provider=token_provider(),
        **extra,
    )


def git(cwd: Path, *args: str) -> str:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    completed = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode().strip()


def make_bare_remote(root: Path) -> tuple[Path, str]:
    work = root / "seed"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("hello\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    sha = git(work, "rev-parse", "HEAD")
    bare = root / "remote.git"
    git(root, "clone", "-q", "--bare", str(work), str(bare))
    return bare, sha


def commit_on_top(repo: Path, name: str) -> str:
    (repo / name).write_text("change\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")
    return git(repo, "rev-parse", "HEAD")
