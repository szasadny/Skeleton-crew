from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from git_helpers import commit_files, git
from githubkit import GitHub

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.settings import LabelNames, Limits
from skeleton_crew.github._git import REMOTE_PROTOCOL, GitRemote
from skeleton_crew.github.gateway import GatewayConfig, GitHubGateway, github_remote_url

API = "https://api.github.com"
REPO = "o/r"
TOKEN = "ghs_test-installation-token-0123456789"  # noqa: S105
SHA = "a" * 40


@dataclass
class AuditLog:
    events: list[AuditEntry] = field(default_factory=list)

    async def __call__(self, entry: AuditEntry) -> None:
        self.events.append(entry)

    def kinds(self) -> list[tuple[AuditKind, AuditOutcome]]:
        return [(entry.kind, entry.outcome) for entry in self.events]


def make_client() -> GitHub[Any]:
    return GitHub("test-client-token", http_cache=False, auto_retry=False)


def make_gateway(
    audit: AuditLog,
    *,
    dry_run: bool = False,
    max_comment_chars: int = Limits().max_comment_chars,
    local_remote: Path | None = None,
) -> GitHubGateway:
    async def remote() -> GitRemote:
        if local_remote is None:
            return GitRemote(github_remote_url(REPO), REMOTE_PROTOCOL, TOKEN)
        return GitRemote(str(local_remote), "file", TOKEN)

    config = GatewayConfig(REPO, LabelNames(), "main", dry_run, max_comment_chars)
    return GitHubGateway(make_client(), config, audit, remote)


def make_bare_remote(root: Path) -> tuple[Path, str]:
    work = root / "seed"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    sha = commit_files(work, {"README.md": "hello\n"}, "init")
    bare = root / "remote.git"
    git(root, "clone", "-q", "--bare", str(work), str(bare))
    return bare, sha


def commit_on_top(repo: Path, name: str) -> str:
    return commit_files(repo, {name: "change\n"}, "change")
