import asyncio
import base64
from pathlib import Path

import pytest
from github_helpers import TOKEN, AuditLog, commit_on_top, git, make_bare_remote, make_gateway

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.github import gateway as gateway_module
from skeleton_crew.github.gateway import GitCommandError, _git_env, github_remote_url, run_git


def test_remote_url_is_https_github() -> None:
    assert github_remote_url("o/r") == "https://github.com/o/r.git"


def test_git_env_scopes_auth_header_to_github_and_hardens_config() -> None:
    env = _git_env(TOKEN)

    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert "GIT_CONFIG_COUNT" not in _git_env(None)


async def test_token_never_reaches_git_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        seen.append(args)
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    await run_git(["--version"], cwd=None, token=TOKEN)

    assert seen
    assert all(TOKEN not in str(arg) for args in seen for arg in args)
    assert seen[0][:5] == ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


async def test_git_failure_redacts_token() -> None:
    with pytest.raises(GitCommandError) as info:
        await run_git(["rev-parse", TOKEN], cwd=None, token=TOKEN)

    assert TOKEN not in str(info.value)


async def test_clone_checks_out_base_and_stores_no_credentials(tmp_path: Path) -> None:
    bare, sha = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(bare))
    dest = tmp_path / "clone"

    assert await gateway.clone(dest) == sha
    config = (dest / ".git" / "config").read_text()
    assert "extraheader" not in config.lower()
    assert TOKEN not in config
    assert git(dest, "tag") == ""


async def test_clone_with_planted_credential_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare, _ = make_bare_remote(tmp_path)
    real_run_git = gateway_module.run_git

    async def run_and_plant(args: list[str], **kwargs: object) -> str:
        out = await real_run_git(args, **kwargs)  # type: ignore[arg-type]
        if args[0] == "clone":
            dest = Path(args[-1])
            git(dest, "config", "http.https://github.com/.extraheader", "AUTHORIZATION: x")
        return out

    monkeypatch.setattr(gateway_module, "run_git", run_and_plant)
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(bare))

    with pytest.raises(PolicyViolation):
        await gateway.clone(tmp_path / "clone")
    assert not (tmp_path / "clone").exists()


async def test_push_branch_force_pushes_skeleton_branch(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, remote_url=lambda _: str(bare))
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    first = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", first)
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    rewritten = commit_on_top(clone, "b.txt")

    await gateway.push_branch(clone, "skeleton/4-fix", rewritten)

    assert git(bare, "rev-parse", "refs/heads/skeleton/4-fix") == rewritten
    assert audit.kinds() == [(AuditKind.GITHUB_WRITE, AuditOutcome.ALLOWED)] * 2


@pytest.mark.parametrize("branch", ["main", "develop", "skeleton/0-x", "skeleton/4-Fix", "x"])
async def test_push_to_base_or_non_skeleton_branch_is_blocked(tmp_path: Path, branch: str) -> None:
    bare, sha = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, remote_url=lambda _: str(bare))
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    new = commit_on_top(clone, "a.txt")

    with pytest.raises(PolicyViolation):
        await gateway.push_branch(clone, branch, new)

    assert git(bare, "rev-parse", "refs/heads/main") == sha
    assert audit.kinds() == [(AuditKind.POLICY_VIOLATION, AuditOutcome.BLOCKED)]


async def test_push_rejects_refspec_injection_through_sha(tmp_path: Path) -> None:
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(tmp_path))

    with pytest.raises(PolicyViolation):
        await gateway.push_branch(tmp_path, "skeleton/4-fix", "HEAD:refs/heads/main")


async def test_dry_run_push_does_not_push(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, dry_run=True, remote_url=lambda _: str(bare))
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    new = commit_on_top(clone, "a.txt")

    await gateway.push_branch(clone, "skeleton/4-fix", new)

    assert "skeleton/4-fix" not in git(bare, "branch", "--list")
    assert audit.kinds() == [(AuditKind.GITHUB_WRITE, AuditOutcome.DRY_RUN)]


async def test_lease_push_updates_the_branch_it_expects(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    audit = AuditLog()
    gateway = make_gateway(audit, remote_url=lambda _: str(bare))
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    first = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", first)
    second = commit_on_top(clone, "b.txt")

    await gateway.push_branch(clone, "skeleton/4-fix", second, expected_sha=first)

    assert git(bare, "rev-parse", "refs/heads/skeleton/4-fix") == second
    payload = audit.events[-1][3]
    assert payload is not None
    assert payload["expected"] == first


async def test_lease_push_refuses_to_overwrite_someone_elses_commit(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(bare))
    clone = tmp_path / "clone"
    await gateway.clone(clone)
    ours = commit_on_top(clone, "a.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", ours)
    human = commit_on_top(clone, "human.txt")
    await gateway.push_branch(clone, "skeleton/4-fix", human)
    git(clone, "reset", "-q", "--hard", ours)
    update = commit_on_top(clone, "b.txt")

    with pytest.raises(GitCommandError):
        await gateway.push_branch(clone, "skeleton/4-fix", update, expected_sha=ours)

    assert git(bare, "rev-parse", "refs/heads/skeleton/4-fix") == human


async def test_lease_push_rejects_a_non_sha_lease(tmp_path: Path) -> None:
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(tmp_path))

    with pytest.raises(PolicyViolation):
        await gateway.push_branch(tmp_path, "skeleton/4-fix", "a" * 40, expected_sha="main")


async def test_fetch_branch_returns_the_remote_head(tmp_path: Path) -> None:
    bare, _ = make_bare_remote(tmp_path)
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(bare))
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
    gateway = make_gateway(AuditLog(), remote_url=lambda _: str(tmp_path))

    with pytest.raises(PolicyViolation):
        await gateway.fetch_branch(tmp_path, branch)
