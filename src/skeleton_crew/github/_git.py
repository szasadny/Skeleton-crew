import base64
import shutil
from collections.abc import Sequence
from pathlib import Path

from skeleton_crew.core.git import run_git
from skeleton_crew.domain.errors import PolicyViolation

GIT_TIMEOUT_S = 900.0
REMOTE_PROTOCOL = "https"
_AUTH_HEADER_KEY = "http.https://github.com/.extraheader"


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


async def _remote_git(
    args: Sequence[str], *, cwd: Path | None, token: str | None, protocol: str
) -> str:
    return await run_git(
        args,
        cwd=cwd,
        timeout_s=GIT_TIMEOUT_S,
        env=auth_env(token),
        protocols=(protocol,),
        secrets=[token] if token else [],
    )


async def _local_git(args: Sequence[str], *, cwd: Path) -> str:
    return await run_git(args, cwd=cwd, timeout_s=GIT_TIMEOUT_S)


def _check_clone_config(dest: Path, token: str | None) -> None:
    config = (dest / ".git" / "config").read_text(errors="replace")
    lowered = config.casefold()
    if "extraheader" in lowered or "authorization" in lowered or (token and token in config):
        shutil.rmtree(dest, ignore_errors=True)
        raise PolicyViolation("clone config contains credentials")


async def git_clone(
    url: str, dest: Path, branch: str, token: str | None, *, protocol: str = REMOTE_PROTOCOL
) -> str:
    await _remote_git(
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
        protocol=protocol,
    )
    _check_clone_config(dest, token)
    return (await _local_git(["rev-parse", "HEAD"], cwd=dest)).strip()


async def git_fetch(
    repo_dir: Path, url: str, branch: str, token: str | None, *, protocol: str = REMOTE_PROTOCOL
) -> str:
    await _remote_git(
        ["fetch", "--quiet", "--no-tags", "--", url, f"refs/heads/{branch}"],
        cwd=repo_dir,
        token=token,
        protocol=protocol,
    )
    return (await _local_git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir)).strip()


async def git_branch_head(
    url: str, branch: str, token: str | None, *, protocol: str = REMOTE_PROTOCOL
) -> str | None:
    ref = f"refs/heads/{branch}"
    out = await _remote_git(
        ["ls-remote", "--heads", "--", url, ref], cwd=None, token=token, protocol=protocol
    )
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return None


async def git_push(
    repo_dir: Path,
    url: str,
    sha: str,
    branch: str,
    token: str | None,
    expected_sha: str | None = None,
    *,
    protocol: str = REMOTE_PROTOCOL,
) -> None:
    ref = f"refs/heads/{branch}"
    # GitHub refuses the push if the branch moved meanwhile; an empty lease means it must not exist.
    lease = f"--force-with-lease={ref}:{expected_sha or ''}"
    await _remote_git(
        ["push", "--quiet", lease, "--no-verify", url, f"{sha}:{ref}"],
        cwd=repo_dir,
        token=token,
        protocol=protocol,
    )
