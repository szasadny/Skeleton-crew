import asyncio
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from skeleton_crew.domain.errors import PolicyViolation

MAX_PATCH_BYTES = 20_000_000
GIT_TIMEOUT_S = 300.0

_TASK_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_AUTHOR_RE = re.compile(r"^(?P<name>[^<>\n\0]*[^<>\s\0])\s*<(?P<email>[^<>\s\0]+)>$")
_HARDENING = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.allow=never",
)

AGENT_PATCH_ARGV: tuple[str, ...] = (
    "sh",
    "-c",
    "git -C /workspace add -A && git -C /workspace -c core.quotepath=off diff --cached --binary"
    " --no-ext-diff --no-textconv --no-color --no-renames --no-relative"
    ' --src-prefix=a/ --dst-prefix=b/ "$0"',
)


class GitCommandError(Exception):
    pass


@dataclass(frozen=True)
class MergeAttempt:
    tree: str
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class TaskPaths:
    root: Path
    controller_repo: Path
    agent_repo: Path
    inbox: Path
    transcripts: Path


def _check_sha(sha: str) -> str:
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    return sha


def _git_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.devnull,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if extra:
        env.update(extra)
    return env


def _git(
    repo: Path,
    *args: str,
    stdin: bytes | None = None,
    stdout: IO[bytes] | None = None,
    env: Mapping[str, str] | None = None,
    exit_codes: frozenset[int] = frozenset({0}),
) -> bytes:
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["git", *_HARDENING, "-C", str(repo), *args],  # noqa: S607 -- git from the controller image
        input=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(env),
        timeout=GIT_TIMEOUT_S,
        check=False,
    )
    if completed.returncode not in exit_codes:
        message = completed.stderr.decode(errors="replace").strip()[-500:]
        raise GitCommandError(f"git {args[0]} failed: {message}")
    return completed.stdout if stdout is None else b""


def task_paths(tasks_dir: Path, task_key: str) -> TaskPaths:
    if not _TASK_KEY_RE.fullmatch(task_key):
        raise ValueError(f"invalid task key: {task_key!r}")
    root = tasks_dir / task_key
    return TaskPaths(
        root=root,
        controller_repo=root / "controller",
        agent_repo=root / "agent",
        inbox=root / "inbox",
        transcripts=root / "transcripts",
    )


def make_agent_copy(paths: TaskPaths, *, fresh: bool = True) -> None:
    if paths.agent_repo.exists():
        if not fresh:
            return
        shutil.rmtree(paths.agent_repo)
    shutil.copytree(paths.controller_repo, paths.agent_repo, symlinks=True)


def _skip_escaping_links(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError:
        return None


def _extract_commit(src_repo: Path, dest: Path, sha: str, *, clear: bool) -> None:
    with tempfile.TemporaryFile() as archive:
        _git(src_repo, "archive", "--format=tar", "--end-of-options", sha, stdout=archive)
        archive.seek(0)
        if clear:
            _clear_dir(dest)
        with tarfile.open(fileobj=archive, mode="r:") as tar:
            tar.extractall(dest, filter=_skip_escaping_links)  # noqa: S202 -- wraps data_filter


def _clear_dir(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def make_readonly_copy(src_repo: Path, dest: Path, sha: str) -> None:
    _check_sha(sha)
    dest.mkdir(parents=True, exist_ok=False)
    _extract_commit(src_repo, dest, sha, clear=False)


def refresh_readonly_copy(src_repo: Path, dest: Path, sha: str) -> None:
    # Replaces the contents in place: a running container bind-mounts this directory.
    _check_sha(sha)
    _extract_commit(src_repo, dest, sha, clear=True)


def _parse_author(author: str) -> tuple[str, str]:
    match = _AUTHOR_RE.fullmatch(author.strip())
    if match is None:
        raise ValueError("author must look like 'Name <email>'")
    return match["name"].strip(), match["email"]


def _commit_tree(repo: Path, tree: str, parents: Sequence[str], message: str, author: str) -> str:
    name, email = _parse_author(author)
    identity = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    parent_args = [arg for sha in parents for arg in ("-p", _check_sha(sha))]
    commit = _git(repo, "commit-tree", tree, *parent_args, "-m", message, env=identity)
    return commit.decode().strip()


def _tree_of(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{sha}^{{tree}}").decode().strip()


async def tree_of(controller_repo: Path, sha: str) -> str:
    return await asyncio.to_thread(_tree_of, controller_repo, _check_sha(sha))


def _commit_patch(
    repo: Path, patch: bytes, base_sha: str, parents: Sequence[str], message: str, author: str
) -> str:
    _parse_author(author)
    with tempfile.TemporaryDirectory() as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", "--end-of-options", f"{base_sha}^{{tree}}", env=index)
        apply = ("apply", "--cached", "--binary", "--whitespace=nowarn")
        try:
            _git(repo, *apply, "--check", "-", stdin=patch, env=index)
        except GitCommandError as exc:
            raise PolicyViolation(f"patch does not apply: {exc}") from exc
        _git(repo, *apply, "-", stdin=patch, env=index)
        tree = _git(repo, "write-tree", env=index).decode().strip()
    if tree == _tree_of(repo, parents[0]):
        raise PolicyViolation("no changes")
    return _commit_tree(repo, tree, parents, message, author)


async def apply_agent_patch(
    controller_repo: Path,
    *,
    patch: bytes,
    base_sha: str,
    parent_sha: str,
    message: str,
    author: str,
    extra_parents: Sequence[str] = (),
) -> str:
    if len(patch) > MAX_PATCH_BYTES:
        raise PolicyViolation(f"patch larger than {MAX_PATCH_BYTES} bytes")
    if not patch.strip():
        raise PolicyViolation("no changes")
    if not message.strip() or "\0" in message:
        raise ValueError("commit message must be non-empty text")
    _check_sha(base_sha)
    parents = [_check_sha(sha) for sha in (parent_sha, *extra_parents)]
    return await asyncio.to_thread(
        _commit_patch, controller_repo, patch, base_sha, parents, message, author
    )


async def commit_tree(
    controller_repo: Path, *, tree: str, parents: Sequence[str], message: str, author: str
) -> str:
    if not message.strip() or "\0" in message:
        raise ValueError("commit message must be non-empty text")
    _check_sha(tree)
    return await asyncio.to_thread(_commit_tree, controller_repo, tree, parents, message, author)


def _merge_trees(repo: Path, ours: str, theirs: str) -> MergeAttempt:
    args = ("merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", ours, theirs)
    # Exit code 1 means the merge has conflicts; the tree then holds the conflict markers.
    out = _git(repo, *args, exit_codes=frozenset({0, 1})).decode(errors="surrogateescape")
    tree, *paths = out.split("\0")
    return MergeAttempt(_check_sha(tree), tuple(dict.fromkeys(p for p in paths if p)))


async def merge_trees(controller_repo: Path, ours: str, theirs: str) -> MergeAttempt:
    _check_sha(ours)
    _check_sha(theirs)
    return await asyncio.to_thread(_merge_trees, controller_repo, ours, theirs)


def _checkout(repo: Path, sha: str) -> None:
    _git(repo, "checkout", "--quiet", "--force", "--detach", sha)


async def checkout_commit(controller_repo: Path, sha: str) -> None:
    await asyncio.to_thread(_checkout, controller_repo, _check_sha(sha))


def _binary_diff(repo: Path, base_sha: str, sha: str) -> bytes:
    return _git(
        repo,
        "-c",
        "core.quotepath=off",
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        base_sha,
        sha,
    )


async def binary_diff(controller_repo: Path, base_sha: str, sha: str) -> bytes:
    _check_sha(base_sha)
    _check_sha(sha)
    return await asyncio.to_thread(_binary_diff, controller_repo, base_sha, sha)


def _diff_summary(repo: Path, base_sha: str, sha: str) -> tuple[str, str, str]:
    common = ("diff", "--no-renames", "--no-ext-diff", "--no-textconv")
    raw = _git(repo, *common, "--raw", "-z", base_sha, sha)
    numstat = _git(repo, *common, "--numstat", "-z", base_sha, sha)
    unified = _git(repo, *common, "-U0", "--no-color", base_sha, sha)
    return (
        raw.decode(errors="surrogateescape"),
        numstat.decode(errors="surrogateescape"),
        unified.decode(errors="surrogateescape"),
    )


async def diff_summary(controller_repo: Path, base_sha: str, sha: str) -> tuple[str, str, str]:
    _check_sha(base_sha)
    _check_sha(sha)
    return await asyncio.to_thread(_diff_summary, controller_repo, base_sha, sha)


def agent_patch_argv(base_sha: str) -> list[str]:
    return [*AGENT_PATCH_ARGV, _check_sha(base_sha)]
