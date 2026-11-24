import base64
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from skeleton_crew.core.config import GITHUB_URL
from skeleton_crew.core.git import RemoteAccess, run_git
from skeleton_crew.domain.errors import PolicyViolation

REMOTE_GIT_TIMEOUT_S = 900.0
REMOTE_PROTOCOL = "https"
_AUTH_HEADER_KEY = f"http.{GITHUB_URL}/.extraheader"


@dataclass(frozen=True)
class GitRemote:
    url: str
    protocol: str
    token: str | None = field(repr=False)


def auth_env(token: str | None) -> dict[str, str]:
    if token is None:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # Env-only config: never on argv (visible in ps) and never written to .git/config.
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": _AUTH_HEADER_KEY,
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


async def _remote_git(args: Sequence[str], remote: GitRemote, *, cwd: Path | None) -> str:
    access = RemoteAccess(
        env=auth_env(remote.token),
        protocols=(remote.protocol,),
        secrets=[remote.token] if remote.token else [],
    )
    return await run_git(args, cwd=cwd, timeout_s=REMOTE_GIT_TIMEOUT_S, remote=access)


async def _local_git(args: Sequence[str], *, cwd: Path) -> str:
    return await run_git(args, cwd=cwd, timeout_s=REMOTE_GIT_TIMEOUT_S)


def _clone_config_problem(dest: Path, token: str | None) -> str | None:
    config = (dest / ".git" / "config").read_text(errors="replace")
    lowered = config.casefold()
    if "extraheader" in lowered or "authorization" in lowered or (token and token in config):
        return "clone config contains credentials"
    return None


async def git_clone(remote: GitRemote, dest: Path, branch: str) -> str:
    args = ["clone", "--quiet", "--no-tags", "--single-branch", "--branch", branch]
    await _remote_git([*args, "--", remote.url, str(dest)], remote, cwd=None)
    problem = _clone_config_problem(dest, remote.token)
    if problem is not None:
        shutil.rmtree(dest, ignore_errors=True)
        raise PolicyViolation(problem)
    return (await _local_git(["rev-parse", "HEAD"], cwd=dest)).strip()


async def git_fetch(repo_dir: Path, remote: GitRemote, branch: str) -> str:
    args = ["fetch", "--quiet", "--no-tags", "--", remote.url, f"refs/heads/{branch}"]
    await _remote_git(args, remote, cwd=repo_dir)
    return (await _local_git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir)).strip()


async def git_branch_head(remote: GitRemote, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    out = await _remote_git(["ls-remote", "--heads", "--", remote.url, ref], remote, cwd=None)
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return None


async def git_push(
    repo_dir: Path, remote: GitRemote, sha: str, branch: str, *, expected_sha: str | None = None
) -> None:
    ref = f"refs/heads/{branch}"
    # GitHub refuses the push if the branch moved meanwhile; an empty lease means it must not exist.
    lease = f"--force-with-lease={ref}:{expected_sha or ''}"
    args = ["push", "--quiet", lease, "--no-verify", remote.url, f"{sha}:{ref}"]
    await _remote_git(args, remote, cwd=repo_dir)
