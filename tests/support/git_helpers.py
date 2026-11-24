import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "HOME": os.devnull,
    "PATH": os.environ.get("PATH", ""),
}


def require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not installed")


def run_command(argv: Sequence[str], cwd: Path, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, env=GIT_ENV, input=stdin, check=True, capture_output=True
    )
    return completed.stdout


def git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    require_git()
    return run_command(["git", *args], cwd, stdin)


def git_text(cwd: Path, *args: str) -> str:
    return git(cwd, *args).decode().strip()


def commit_files(repo: Path, files: Mapping[str, str], message: str) -> str:
    for path, content in files.items():
        (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git_text(repo, "rev-parse", "HEAD")


class GitRepo:
    def __init__(self, root: Path, *, init: bool = True) -> None:
        require_git()
        self.root = root
        if init:
            root.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q")

    def git(self, *args: str, stdin: bytes | None = None) -> bytes:
        return git(self.root, *args, stdin=stdin)

    def write(self, path: str, content: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def commit(self, message: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        return git_text(self.root, "rev-parse", "HEAD")

    def show(self, sha: str, path: str) -> bytes:
        return self.git("show", f"{sha}:{path}")

    def raw(self, base: str, head: str) -> str:
        return self.git("diff", "--no-renames", "--raw", "-z", base, head).decode()

    def numstat(self, base: str, head: str) -> str:
        return self.git("diff", "--no-renames", "--numstat", "-z", base, head).decode()

    def unified(self, base: str, head: str) -> str:
        return self.git("diff", "--no-renames", "-U0", "--no-color", base, head).decode()
