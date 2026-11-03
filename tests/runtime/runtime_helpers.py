import os
import shutil
import subprocess
from collections.abc import Sequence
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


def run(argv: Sequence[str], cwd: Path, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, env=GIT_ENV, input=stdin, check=True, capture_output=True
    )
    return completed.stdout


class Repo:
    def __init__(self, root: Path, *, init: bool = True) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        self.root = root
        if init:
            root.mkdir(parents=True, exist_ok=True)
            self.git("init", "-q")

    def git(self, *args: str, stdin: bytes | None = None) -> bytes:
        return run(["git", *args], self.root, stdin)

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
        return self.git("rev-parse", "HEAD").decode().strip()

    def show(self, sha: str, path: str) -> bytes:
        return self.git("show", f"{sha}:{path}")
