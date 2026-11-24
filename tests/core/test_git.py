import os
from pathlib import Path

import pytest
from git_helpers import git

from skeleton_crew.core.git import HARDENING, GitCommandError, git_argv, git_env, run_git_sync


def _seed(root: Path) -> Path:
    work = root / "seed"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "commit", "-q", "--allow-empty", "-m", "init")
    return work


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
