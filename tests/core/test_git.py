import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from skeleton_crew.core.git import HARDENING, GitCommandError, git_argv, git_env, run_git_sync
from skeleton_crew.github._git import auth_env, git_clone
from skeleton_crew.runtime import workspace

SECRET = "ghs_secret-token-0123456789"  # noqa: S105
_AUTH_KEYS = set(auth_env(SECRET))


def _seed(root: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    env = {
        **git_env(),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    work = root / "seed"
    work.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=work, env=env, check=True)  # noqa: S603, S607
    return work


async def test_gateway_and_workspace_run_git_with_the_same_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = _seed(tmp_path)
    sync_calls: list[tuple[list[str], Mapping[str, str]]] = []
    async_calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
    real_run = subprocess.run
    real_exec = asyncio.create_subprocess_exec

    def spy_run(argv: list[str], **kwargs: Any) -> Any:
        sync_calls.append((argv, kwargs["env"]))
        return real_run(argv, **kwargs)

    async def spy_exec(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        async_calls.append((argv, kwargs["env"]))
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

    clone = tmp_path / "clone"
    sha = await git_clone(str(seed), clone, "main", SECRET, protocol="file")
    await workspace.tree_of(clone, sha)

    assert sync_calls
    assert async_calls
    prefix = ["git", *HARDENING]
    for argv, env in [*sync_calls, *((list(a), e) for a, e in async_calls)]:
        assert argv[: len(prefix)] == prefix
        assert {k: v for k, v in env.items() if k not in _AUTH_KEYS} == git_env()
    assert all(SECRET not in arg for argv, _ in async_calls for arg in argv)


def test_env_pins_isolation_settings_that_extra_env_cannot_drop() -> None:
    env = git_env({"GIT_INDEX_FILE": "index.tmp", "HOME": "/root", "GIT_CONFIG_NOSYSTEM": "0"})

    assert env["HOME"] == os.devnull
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["LC_ALL"] == "C"
    assert env["GIT_INDEX_FILE"] == "index.tmp"
    assert set(env) == {*git_env(), "GIT_INDEX_FILE"}


def test_hardening_disables_hooks_fsmonitor_and_all_transports() -> None:
    assert git_argv(["status"]) == [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        "status",
    ]


def test_protocol_allowlist_is_added_after_the_global_deny() -> None:
    argv = git_argv(["fetch"], protocols=["https"])

    assert argv[: 1 + len(HARDENING)] == ["git", *HARDENING]
    assert argv[1 + len(HARDENING) :] == ["-c", "protocol.https.allow=always", "fetch"]


def test_workspace_git_refuses_every_transport(tmp_path: Path) -> None:
    seed = _seed(tmp_path)

    with pytest.raises(GitCommandError, match="transport 'file' not allowed"):
        run_git_sync(["ls-remote", str(seed)], cwd=tmp_path, timeout_s=30)
