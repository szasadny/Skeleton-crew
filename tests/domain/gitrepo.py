import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "HOME": os.devnull,
    "PATH": os.environ.get("PATH", ""),
}


class GitRepo:
    def __init__(self, root: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        self.root = root
        self.git("init", "-q")

    def git(self, *args: str) -> str:
        completed = subprocess.run(  # noqa: S603
            ["git", "-c", "core.quotePath=true", *args],  # noqa: S607
            cwd=self.root,
            env=_ENV,
            check=True,
            capture_output=True,
        )
        return completed.stdout.decode()

    def write(self, path: str, content: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "c")
        return self.git("rev-parse", "HEAD").strip()

    def raw(self, base: str, head: str) -> str:
        return self.git("diff", "--no-renames", "--raw", "-z", base, head)

    def numstat(self, base: str, head: str) -> str:
        return self.git("diff", "--no-renames", "--numstat", "-z", base, head)

    def unified(self, base: str, head: str) -> str:
        return self.git("diff", "--no-renames", "-U0", "--no-color", base, head)
