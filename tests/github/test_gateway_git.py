import asyncio
import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from git_helpers import git_text
from github_helpers import TOKEN, AuditLog, commit_on_top, make_bare_remote, make_gateway

from skeleton_crew.core import git as core_git
from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.core.git import HARDENING, GitCommandError, git_env
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.github import _git as git_module
from skeleton_crew.github._git import (
    REMOTE_PROTOCOL,
    GitRemote,
    auth_env,
    git_branch_head,
    git_clone,
)
from skeleton_crew.github.gateway import github_remote_url


def _local(path: Path | str, token: str | None = TOKEN, protocol: str = "file") -> GitRemote:
    return GitRemote(str(path), protocol, token)


def test_remote_url_is_https_github() -> None:
    assert github_remote_url("o/r") == "https://github.com/o/r.git"


def test_auth_env_scopes_auth_header_to_github() -> None:
    env = auth_env(TOKEN)

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert auth_env(None) == {}


async def test_token_never_reaches_git_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        seen.append(args)
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    bare, _ = make_bare_remote(tmp_path)
    await git_branch_head(_local(bare), "main")

    assert seen
    assert all(TOKEN not in str(arg) for args in seen for arg in args)
    assert seen[0][: 1 + len(HARDENING)] == ("git", *HARDENING)


async def test_git_failure_redacts_token(tmp_path: Path) -> None:
    with pytest.raises(GitCommandError) as info:
        await git_branch_head(_local(tmp_path / TOKEN), "main")

    assert "failed" in str(info.value)
    assert TOKEN not in str(info.value)


async def test_https_remote_refuses_every_other_transport(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)

    with pytest.raises(GitCommandError, match="transport 'file' not allowed"):
        await git_clone(_local(bare, protocol=REMOTE_PROTOCOL), tmp_path / "clone", "main")
    ext = _local("ext::sh -c touch% /tmp/pwned", None, REMOTE_PROTOCOL)
    with pytest.raises(GitCommandError, match="transport 'ext' not allowed"):
        await git_branch_head(ext, "main")


@pytest.mark.parametrize("protocol", ["", "file -c x=y", "file.allow", "Ext", "a=b"])
async def test_remote_git_rejects_malformed_protocol(tmp_path: Path, protocol: str) -> None:
    with pytest.raises(ValueError, match="protocol"):
        await git_branch_head(_local(tmp_path, None, protocol), "main")


def test_remote_repr_hides_the_token() -> None:
    assert TOKEN not in repr(_local("/srv/remote.git"))


async def test_remote_git_runs_with_the_core_hardening_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append((argv, kwargs["env"]))
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    bare, _ = make_bare_remote(tmp_path)

    await git_clone(_local(bare), tmp_path / "clone", "main")

    assert calls
    auth_keys = set(auth_env(TOKEN))
    for argv, env in calls:
        assert argv[: 1 + len(HARDENING)] == ("git", *HARDENING)
        assert {k: v for k, v in env.items() if k not in auth_keys} == git_env()


async def test_clone_checks_out_base_and_stores_no_credentials(tmp_path: Path) -> None:
    bare, sha = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), local_remote=bare)
    dest = tmp_path / "clone"

    assert await gateway.clone(dest) == sha
    config = (dest / ".git" / "config").read_text()
    assert "extraheader" not in config.lower()
    assert TOKEN not in config
    assert git_text(dest, "tag") == ""


async def test_clone_with_planted_credential_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, _ = make_bare_remote(tmp_path)
    real_run_git = core_git.run_git

    async def run_and_plant(args: list[str], **kwargs: Any) -> str:
        out = await real_run_git(args, **kwargs)
        if args[0] == "clone":
            dest = Path(args[-1])
            git_text(dest, "config", "http.https://github.com/.extraheader", "AUTHORIZATION: x")
        return out

    monkeypatch.setattr(git_module, "run_git", run_and_plant)
    gateway = make_gateway(AuditLog(), local_remote=bare)

    with pytest.raises(PolicyViolation):
        await gateway.clone(tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


async def test_push_branch_rewrites_the_branch_it_leased(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    first = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", first)
    git_text(clone, "reset", "-q", "--hard", "HEAD~1")
    rewritten = commit_on_top(clone, "b.txt")

    await gateway.push_branch(clone, "skeleton/4-fix", rewritten, expected_sha=first)

    assert git_text(bare, "rev-parse", "refs/heads/skeleton/4-fix") == rewritten
    assert audit.kinds() == [(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED)] * 2


async def test_push_without_lease_never_overwrites_an_existing_branch(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    human = commit_on_top(clone, "human.txt")
    git_text(clone, "push", "-q", str(bare), f"{human}:refs/heads/skeleton/4-fix")
    git_text(clone, "reset", "-q", "--hard", "HEAD~1")
    ours = commit_on_top(clone, "a.txt")

    with pytest.raises(GitCommandError):
        await gateway.push_branch(clone, "skeleton/4-fix", ours)

    assert git_text(bare, "rev-parse", "refs/heads/skeleton/4-fix") == human


async def test_branch_head_reads_only_the_exact_branch(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    longer = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix-more", longer)

    assert await gateway.branch_head("skeleton/4-fix-more") == longer
    assert await gateway.branch_head("skeleton/4-fix") is None


@pytest.mark.parametrize("branch", ["main", "--upload-pack=x", "skeleton/4-fix:refs/heads/main"])
async def test_branch_head_only_accepts_skeleton_branches(tmp_path: Path, branch: str) -> None:
    gateway = make_gateway(AuditLog(), local_remote=tmp_path)

    with pytest.raises(PolicyViolation):
        await gateway.branch_head(branch)


@pytest.mark.parametrize("branch", ["main", "develop", "skeleton/0-x", "skeleton/4-Fix", "x"])
async def test_push_to_base_or_non_skeleton_branch_is_blocked(tmp_path: Path, branch: str) -> None:
    bare, sha = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    new = commit_on_top(clone, "a.txt")

    with pytest.raises(PolicyViolation):
        await gateway.push_branch(clone, branch, new)

    assert git_text(bare, "rev-parse", "refs/heads/main") == sha
    assert audit.kinds() == [(AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED)]


async def test_push_rejects_refspec_injection_through_sha(tmp_path: Path) -> None:
    gateway = make_gateway(AuditLog(), local_remote=tmp_path)

    with pytest.raises(PolicyViolation):
        await gateway.push_branch(tmp_path, "skeleton/4-fix", "HEAD:refs/heads/main")


async def test_dry_run_push_does_not_push(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, dry_run=True, local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    new = commit_on_top(clone, "a.txt")

    await gateway.push_branch(clone, "skeleton/4-fix", new)

    assert "skeleton/4-fix" not in git_text(bare, "branch", "--list")
    assert audit.kinds() == [(AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN)]


async def test_lease_push_updates_the_branch_it_expects(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    first = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", first)
    second = commit_on_top(clone, "b.txt")

    await gateway.push_branch(clone, "skeleton/4-fix", second, expected_sha=first)

    assert git_text(bare, "rev-parse", "refs/heads/skeleton/4-fix") == second
    payload = audit.events[-1].payload
    assert payload is not None
    assert payload["expected"] == first


async def test_lease_push_refuses_to_overwrite_someone_elses_commit(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    ours = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", ours)
    human = commit_on_top(clone, "human.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", human, expected_sha=ours)
    git_text(clone, "reset", "-q", "--hard", ours)
    update = commit_on_top(clone, "b.txt")

    with pytest.raises(GitCommandError):
        await gateway.push_branch(clone, "skeleton/4-fix", update, expected_sha=ours)

    assert git_text(bare, "rev-parse", "refs/heads/skeleton/4-fix") == human


async def test_lease_push_rejects_a_non_sha_lease(tmp_path: Path) -> None:
    gateway = make_gateway(AuditLog(), local_remote=tmp_path)

    with pytest.raises(PolicyViolation):
        await gateway.push_branch(tmp_path, "skeleton/4-fix", "a" * 40, expected_sha="main")


async def test_fetch_branch_returns_the_remote_head(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), local_remote=bare)
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    pushed = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", pushed)
    fresh = tmp_path / "fresh"
    await gateway.clone(fresh)

    assert await gateway.fetch_branch(fresh, "skeleton/4-fix") == pushed
    assert TOKEN not in (fresh / ".git" / "config").read_text()


@pytest.mark.parametrize("branch", ["main", "--upload-pack=x", "skeleton/4-fix:refs/heads/main"])
async def test_fetch_only_accepts_skeleton_branches(tmp_path: Path, branch: str) -> None:
    gateway = make_gateway(AuditLog(), local_remote=tmp_path)

    with pytest.raises(PolicyViolation):
        await gateway.fetch_branch(tmp_path, branch)
