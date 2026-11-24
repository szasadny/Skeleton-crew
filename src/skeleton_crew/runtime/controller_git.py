import asyncio
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from skeleton_crew.core.git import GitCommandError, GitIo, run_git_sync
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.git_refs import is_full_sha
from skeleton_crew.runtime.containers import WORKSPACE
from skeleton_crew.runtime.task_dirs import TaskPaths

MAX_PATCH_BYTES = 20_000_000
LOCAL_GIT_TIMEOUT_S = 300.0
_SAFE_DIFF_ARGS = ("--no-ext-diff", "--no-textconv", "--no-renames")
_PATCH_ARGS = ("--binary", *_SAFE_DIFF_ARGS, "--no-color", "--src-prefix=a/", "--dst-prefix=b/")
_AUTHOR_RE = re.compile(r"^(?P<name>[^<>\n\0]*[^<>\s\0])\s*<(?P<email>[^<>\s\0]+)>$")
_PIPED = GitIo()

AGENT_PATCH_ARGV: tuple[str, ...] = (
    "sh",
    "-c",
    f"git -C {WORKSPACE} add -A && git -C {WORKSPACE} -c core.quotepath=off diff --cached"
    f' {" ".join(_PATCH_ARGS)} --no-relative "$0"',
)


@dataclass(frozen=True)
class MergeAttempt:
    tree: str
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class DiffSummary:
    raw: str
    numstat: str
    unified: str


def _check_sha(sha: str) -> str:
    if not is_full_sha(sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    return sha


def _identity(author: str) -> dict[str, str]:
    match = _AUTHOR_RE.fullmatch(author.strip())
    if match is None:
        raise ValueError("author must look like 'Name <email>'")
    name, email = match["name"].strip(), match["email"]
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


@dataclass(frozen=True)
class CommitSpec:
    parents: tuple[str, ...]
    message: str
    author: str
    identity: Mapping[str, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.message.strip() or "\0" in self.message:
            raise ValueError("commit message must be non-empty text")
        if not self.parents:
            raise ValueError("a commit needs a parent")
        for sha in self.parents:
            _check_sha(sha)
        object.__setattr__(self, "identity", _identity(self.author))


def _git(repo: Path, *args: str, env: Mapping[str, str] | None = None, io: GitIo = _PIPED) -> bytes:
    return run_git_sync(args, cwd=repo, timeout_s=LOCAL_GIT_TIMEOUT_S, env=env, io=io)


def make_agent_copy(paths: TaskPaths) -> None:
    if paths.agent_repo.exists():
        shutil.rmtree(paths.agent_repo)
    shutil.copytree(paths.controller_repo, paths.agent_repo, symlinks=True)


def _skip_escaping_links(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError:
        return None


def _extract_commit(src_repo: Path, dest: Path, sha: str, *, clear: bool) -> None:
    with tempfile.TemporaryFile() as archive:
        _git(src_repo, "archive", "--format=tar", "--end-of-options", sha, io=GitIo(stdout=archive))
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


def _commit_tree(repo: Path, tree: str, commit: CommitSpec) -> str:
    parent_args = [arg for sha in commit.parents for arg in ("-p", sha)]
    out = _git(repo, "commit-tree", tree, *parent_args, "-m", commit.message, env=commit.identity)
    return out.decode().strip()


def _tree_of(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{sha}^{{tree}}").decode().strip()


async def tree_of(controller_repo: Path, sha: str) -> str:
    return await asyncio.to_thread(_tree_of, controller_repo, _check_sha(sha))


def _commit_patch(repo: Path, patch: bytes, base_sha: str, commit: CommitSpec) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", "--end-of-options", f"{base_sha}^{{tree}}", env=index)
        apply = ("apply", "--cached", "--binary", "--whitespace=nowarn")
        stdin = GitIo(stdin=patch)
        try:
            _git(repo, *apply, "--check", "-", env=index, io=stdin)
        except GitCommandError as exc:
            raise PolicyViolation(f"patch does not apply: {exc}") from exc
        _git(repo, *apply, "-", env=index, io=stdin)
        tree = _git(repo, "write-tree", env=index).decode().strip()
    if tree == _tree_of(repo, commit.parents[0]):
        raise PolicyViolation("no changes")
    return _commit_tree(repo, tree, commit)


async def apply_agent_patch(
    controller_repo: Path, *, patch: bytes, base_sha: str, commit: CommitSpec
) -> str:
    if len(patch) > MAX_PATCH_BYTES:
        raise PolicyViolation(f"patch larger than {MAX_PATCH_BYTES} bytes")
    if not patch.strip():
        raise PolicyViolation("no changes")
    _check_sha(base_sha)
    return await asyncio.to_thread(_commit_patch, controller_repo, patch, base_sha, commit)


async def commit_tree(controller_repo: Path, *, tree: str, commit: CommitSpec) -> str:
    _check_sha(tree)
    return await asyncio.to_thread(_commit_tree, controller_repo, tree, commit)


def _merge_trees(repo: Path, ours: str, theirs: str) -> MergeAttempt:
    args = ("merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", ours, theirs)
    # Exit code 1 means the merge has conflicts; the tree then holds the conflict markers.
    conflicts_ok = GitIo(exit_codes=frozenset({0, 1}))
    out = _git(repo, *args, io=conflicts_ok).decode(errors="surrogateescape")
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
    return _git(repo, "-c", "core.quotepath=off", "diff", *_PATCH_ARGS, base_sha, sha)


async def binary_diff(controller_repo: Path, base_sha: str, sha: str) -> bytes:
    _check_sha(base_sha)
    _check_sha(sha)
    return await asyncio.to_thread(_binary_diff, controller_repo, base_sha, sha)


def _diff_text(repo: Path, base_sha: str, sha: str, *args: str) -> str:
    out = _git(repo, "diff", *_SAFE_DIFF_ARGS, *args, base_sha, sha)
    return out.decode(errors="surrogateescape")


def _diff_summary(repo: Path, base_sha: str, sha: str) -> DiffSummary:
    return DiffSummary(
        raw=_diff_text(repo, base_sha, sha, "--raw", "-z"),
        numstat=_diff_text(repo, base_sha, sha, "--numstat", "-z"),
        unified=_diff_text(repo, base_sha, sha, "-U0", "--no-color"),
    )


async def diff_summary(controller_repo: Path, base_sha: str, sha: str) -> DiffSummary:
    _check_sha(base_sha)
    _check_sha(sha)
    return await asyncio.to_thread(_diff_summary, controller_repo, base_sha, sha)


def agent_patch_argv(base_sha: str) -> list[str]:
    return [*AGENT_PATCH_ARGV, _check_sha(base_sha)]


def _head(repo: Path) -> str:
    return _check_sha(_git(repo, "rev-parse", "--verify", "HEAD").decode().strip())


async def head_commit(controller_repo: Path) -> str:
    return await asyncio.to_thread(_head, controller_repo)
