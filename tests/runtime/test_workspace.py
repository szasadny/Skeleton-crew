import shutil
from pathlib import Path

import pytest
from runtime_helpers import Repo, run

from skeleton_crew.domain.diff_policy import parse_diff_summary
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.runtime.workspace import (
    MAX_PATCH_BYTES,
    TaskPaths,
    agent_patch_argv,
    apply_agent_patch,
    binary_diff,
    checkout_commit,
    commit_tree,
    diff_summary,
    make_agent_copy,
    make_readonly_copy,
    merge_trees,
    refresh_readonly_copy,
    task_paths,
)

AUTHOR = "Skeleton Crew <crew@example.invalid>"


@pytest.fixture
def paths(tmp_path: Path) -> TaskPaths:
    return task_paths(tmp_path / "tasks", "w-1")


@pytest.fixture
def controller(paths: TaskPaths) -> Repo:
    repo = Repo(paths.controller_repo)
    repo.write("README.md", "hello\n")
    repo.write("src/app.py", "print('a')\n")
    repo.commit()
    return repo


def agent_patch(paths: TaskPaths, base: str) -> bytes:
    argv = [arg.replace("/workspace", str(paths.agent_repo)) for arg in agent_patch_argv(base)]
    return run(argv, paths.agent_repo)


def agent_repo(paths: TaskPaths) -> Repo:
    make_agent_copy(paths)
    return Repo(paths.agent_repo, init=False)


@pytest.mark.parametrize("key", ["../x", "a/b", "", ".hidden", "x" * 65])
def test_task_paths_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="task key"):
        task_paths(tmp_path, key)


def test_task_paths_layout(tmp_path: Path) -> None:
    paths = task_paths(tmp_path, "w-7")
    assert paths.root == tmp_path / "w-7"
    assert paths.controller_repo.parent == paths.agent_repo.parent == paths.root


def test_agent_copy_is_independent_and_keeps_symlinks(paths: TaskPaths, controller: Repo) -> None:
    (paths.controller_repo / "link").symlink_to("README.md")
    make_agent_copy(paths)
    assert (paths.agent_repo / ".git").is_dir()
    assert (paths.agent_repo / "link").is_symlink()
    (paths.agent_repo / "README.md").write_text("changed\n")
    assert (paths.controller_repo / "README.md").read_text() == "hello\n"


def test_agent_copy_fresh_replaces_and_resume_keeps(paths: TaskPaths, controller: Repo) -> None:
    make_agent_copy(paths)
    (paths.agent_repo / "work.txt").write_text("in progress\n")
    make_agent_copy(paths, fresh=False)
    assert (paths.agent_repo / "work.txt").exists()
    make_agent_copy(paths, fresh=True)
    assert not (paths.agent_repo / "work.txt").exists()


def test_readonly_copy_has_tree_at_sha_without_git(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    controller.write("README.md", "later\n")
    controller.commit()
    dest = tmp_path / "ro"
    make_readonly_copy(paths.controller_repo, dest, base)
    assert (dest / "README.md").read_text() == "hello\n"
    assert (dest / "src" / "app.py").exists()
    assert not (dest / ".git").exists()


def test_readonly_copy_drops_links_escaping_dest(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    root = paths.controller_repo
    (root / "inside").symlink_to("README.md")
    (root / "absolute").symlink_to("/etc/passwd")
    (root / "src" / "climb").symlink_to("../../../../outside")
    sha = controller.commit()
    dest = tmp_path / "ro"
    make_readonly_copy(root, dest, sha)
    assert (dest / "inside").is_symlink()
    assert not (dest / "absolute").is_symlink()
    assert not (dest / "src" / "climb").is_symlink()
    assert not (tmp_path / "outside").exists()


def test_readonly_copy_rejects_option_like_sha(tmp_path: Path, controller: Repo) -> None:
    with pytest.raises(ValueError, match="sha"):
        make_readonly_copy(controller.root, tmp_path / "ro", "--output=/tmp/x")


def test_readonly_copy_refuses_existing_dest(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    (tmp_path / "ro").mkdir()
    with pytest.raises(FileExistsError):
        make_readonly_copy(paths.controller_repo, tmp_path / "ro", controller.head())


def test_refresh_replaces_contents_in_place(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    first = controller.head()
    (paths.controller_repo / "src" / "app.py").unlink()
    controller.write("src/new.py", "print('b')\n")
    controller.write("README.md", "later\n")
    second = controller.commit()
    dest = tmp_path / "ro"
    make_readonly_copy(paths.controller_repo, dest, first)
    (dest / "stray").mkdir()
    (dest / "stray-link").symlink_to(tmp_path)
    inode = dest.stat().st_ino
    refresh_readonly_copy(paths.controller_repo, dest, second)
    assert dest.stat().st_ino == inode
    assert sorted(p.name for p in dest.rglob("*")) == ["README.md", "new.py", "src"]
    assert (dest / "README.md").read_text() == "later\n"
    assert tmp_path.is_dir()


def test_refresh_drops_links_escaping_dest(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    dest = tmp_path / "ro"
    make_readonly_copy(paths.controller_repo, dest, controller.head())
    (paths.controller_repo / "absolute").symlink_to("/etc/passwd")
    refresh_readonly_copy(paths.controller_repo, dest, controller.commit())
    assert not (dest / "absolute").is_symlink()


def test_refresh_rejects_option_like_sha_and_keeps_contents(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    dest = tmp_path / "ro"
    make_readonly_copy(paths.controller_repo, dest, controller.head())
    with pytest.raises(ValueError, match="sha"):
        refresh_readonly_copy(paths.controller_repo, dest, "--output=/tmp/x")
    assert (dest / "README.md").exists()


async def test_apply_patch_commits_on_parent_with_controller_identity(
    paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.write("src/app.py", "print('b')\n")
    agent.write("new.txt", "new\n")
    patch = agent_patch(paths, base)

    sha = await apply_agent_patch(
        paths.controller_repo,
        patch=patch,
        base_sha=base,
        parent_sha=base,
        message="Fix #1",
        author=AUTHOR,
    )

    assert controller.git("rev-parse", f"{sha}^").decode().strip() == base
    assert controller.show(sha, "src/app.py") == b"print('b')\n"
    assert controller.show(sha, "new.txt") == b"new\n"
    meta = controller.git("log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", sha).decode().strip()
    assert meta == f"{AUTHOR}|{AUTHOR}|Fix #1"
    assert controller.head() == base


async def test_apply_patch_ignores_identity_in_patch_headers(
    paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.write("README.md", "hi\n")
    patch = b"From: Mallory <mallory@example.invalid>\nSubject: x\n\n" + agent_patch(paths, base)
    sha = await apply_agent_patch(
        paths.controller_repo,
        patch=patch,
        base_sha=base,
        parent_sha=base,
        message="m",
        author=AUTHOR,
    )
    assert b"mallory" not in controller.git("log", "-1", "--format=%an%ae%cn%ce", sha)


async def test_follow_up_patch_is_full_diff_from_base_on_previous_tip(
    paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.write("a.txt", "one\n")
    first = await apply_agent_patch(
        paths.controller_repo,
        patch=agent_patch(paths, base),
        base_sha=base,
        parent_sha=base,
        message="1",
        author=AUTHOR,
    )
    agent.write("b.txt", "two\n")
    second = await apply_agent_patch(
        paths.controller_repo,
        patch=agent_patch(paths, base),
        base_sha=base,
        parent_sha=first,
        message="2",
        author=AUTHOR,
    )
    assert controller.git("rev-parse", f"{second}^").decode().strip() == first
    assert controller.show(second, "a.txt") == b"one\n"
    assert controller.show(second, "b.txt") == b"two\n"


async def test_apply_patch_with_binary_file(paths: TaskPaths, controller: Repo) -> None:
    base = controller.head()
    blob = bytes(range(256)) * 8
    agent = agent_repo(paths)
    agent.write("img.bin", blob)
    patch = agent_patch(paths, base)
    assert b"GIT binary patch" in patch
    sha = await apply_agent_patch(
        paths.controller_repo,
        patch=patch,
        base_sha=base,
        parent_sha=base,
        message="m",
        author=AUTHOR,
    )
    assert controller.show(sha, "img.bin") == blob


async def test_patch_touching_git_dir_is_rejected(paths: TaskPaths, controller: Repo) -> None:
    base = controller.head()
    patch = (
        b"diff --git a/.git/config b/.git/config\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/.git/config\n"
        b"@@ -0,0 +1,2 @@\n"
        b"+[core]\n"
        b"+\thooksPath = /tmp/evil\n"
    )
    with pytest.raises(PolicyViolation, match="does not apply"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=patch,
            base_sha=base,
            parent_sha=base,
            message="m",
            author=AUTHOR,
        )
    assert "evil" not in (paths.controller_repo / ".git" / "config").read_text()


async def test_patch_escaping_repo_is_rejected(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    patch = (
        b"diff --git a/../../escape.txt b/../../escape.txt\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/../../escape.txt\n"
        b"@@ -0,0 +1 @@\n"
        b"+x\n"
    )
    with pytest.raises(PolicyViolation, match="does not apply"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=patch,
            base_sha=base,
            parent_sha=base,
            message="m",
            author=AUTHOR,
        )


async def test_patch_that_does_not_apply_is_rejected(paths: TaskPaths, controller: Repo) -> None:
    base = controller.head()
    patch = (
        b"diff --git a/README.md b/README.md\n"
        b"--- a/README.md\n"
        b"+++ b/README.md\n"
        b"@@ -1 +1 @@\n"
        b"-not the content\n"
        b"+replacement\n"
    )
    with pytest.raises(PolicyViolation, match="does not apply"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=patch,
            base_sha=base,
            parent_sha=base,
            message="m",
            author=AUTHOR,
        )


@pytest.mark.parametrize("patch", [b"", b"\n  \n"])
async def test_empty_patch_is_no_changes(paths: TaskPaths, controller: Repo, patch: bytes) -> None:
    base = controller.head()
    with pytest.raises(PolicyViolation, match="no changes"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=patch,
            base_sha=base,
            parent_sha=base,
            message="m",
            author=AUTHOR,
        )


async def test_patch_equal_to_parent_tree_is_no_changes(paths: TaskPaths, controller: Repo) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.write("a.txt", "one\n")
    patch = agent_patch(paths, base)
    first = await apply_agent_patch(
        paths.controller_repo,
        patch=patch,
        base_sha=base,
        parent_sha=base,
        message="1",
        author=AUTHOR,
    )
    with pytest.raises(PolicyViolation, match="no changes"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=patch,
            base_sha=base,
            parent_sha=first,
            message="2",
            author=AUTHOR,
        )


async def test_oversized_patch_is_rejected(paths: TaskPaths, controller: Repo) -> None:
    base = controller.head()
    with pytest.raises(PolicyViolation, match="larger"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=b"x" * (MAX_PATCH_BYTES + 1),
            base_sha=base,
            parent_sha=base,
            message="m",
            author=AUTHOR,
        )


@pytest.mark.parametrize("author", ["no email", "<x@y>", "A <a@b>\nB <c@d>", "A <a b@c>"])
async def test_malformed_author_is_rejected(
    paths: TaskPaths, controller: Repo, author: str
) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.write("a.txt", "one\n")
    with pytest.raises(ValueError, match="author"):
        await apply_agent_patch(
            paths.controller_repo,
            patch=agent_patch(paths, base),
            base_sha=base,
            parent_sha=base,
            message="m",
            author=author,
        )


async def test_controller_hooks_never_run(
    tmp_path: Path, paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    marker = tmp_path / "hook-ran"
    agent = agent_repo(paths)
    agent.write("a.txt", "one\n")
    patch = agent_patch(paths, base)
    hooks = paths.controller_repo / ".git" / "hooks"
    for name in ("pre-commit", "post-commit", "post-index-change", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    await apply_agent_patch(
        paths.controller_repo,
        patch=patch,
        base_sha=base,
        parent_sha=base,
        message="m",
        author=AUTHOR,
    )
    assert not marker.exists()


async def test_agent_repo_config_cannot_change_patch_prefixes(
    paths: TaskPaths, controller: Repo
) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.git("config", "diff.noprefix", "true")
    agent.git("config", "diff.mnemonicPrefix", "true")
    agent.write("README.md", "agent\n")
    sha = await apply_agent_patch(
        paths.controller_repo,
        patch=agent_patch(paths, base),
        base_sha=base,
        parent_sha=base,
        message="m",
        author=AUTHOR,
    )
    assert controller.show(sha, "README.md") == b"agent\n"


async def test_diff_summary_feeds_domain_parser(paths: TaskPaths, controller: Repo) -> None:
    base = controller.head()
    agent = agent_repo(paths)
    agent.write("src/app.py", "print('b')\n")
    (paths.agent_repo / "README.md").unlink()
    sha = await apply_agent_patch(
        paths.controller_repo,
        patch=agent_patch(paths, base),
        base_sha=base,
        parent_sha=base,
        message="m",
        author=AUTHOR,
    )
    raw, numstat, unified = await diff_summary(paths.controller_repo, base, sha)
    changes = {c.path: c.status.value for c in parse_diff_summary(raw, numstat)}
    assert changes == {"README.md": "D", "src/app.py": "M"}
    assert "+print('b')" in unified


def test_agent_patch_argv_rejects_non_sha() -> None:
    with pytest.raises(ValueError, match="sha"):
        agent_patch_argv("HEAD; rm -rf /")


def test_git_available() -> None:
    assert shutil.which("git") is not None


def _branch_and_main(controller: Repo, branch_file: str, main_file: str) -> tuple[str, str]:
    base = controller.head()
    controller.write(*branch_file.split("=", 1))
    branch = controller.commit("branch")
    controller.git("checkout", "-q", "--detach", base)
    controller.write(*main_file.split("=", 1))
    main = controller.commit("main")
    return branch, main


async def test_clean_merge_tree_and_two_parent_commit(controller: Repo) -> None:
    branch, main = _branch_and_main(controller, "src/app.py=print('b')\n", "README.md=main\n")

    attempt = await merge_trees(controller.root, branch, main)
    merged = await commit_tree(
        controller.root, tree=attempt.tree, parents=[branch, main], message="m", author=AUTHOR
    )

    assert attempt.conflicts == ()
    parents = controller.git("rev-list", "--parents", "-n", "1", merged).decode().split()[1:]
    assert parents == [branch, main]
    assert controller.show(merged, "src/app.py") == b"print('b')\n"
    assert controller.show(merged, "README.md") == b"main\n"


async def test_conflicting_merge_tree_lists_files_and_keeps_markers(controller: Repo) -> None:
    branch, main = _branch_and_main(
        controller, "src/app.py=print('b')\n", "src/app.py=print('m')\n"
    )

    attempt = await merge_trees(controller.root, branch, main)

    assert attempt.conflicts == ("src/app.py",)
    wip = await commit_tree(
        controller.root, tree=attempt.tree, parents=[branch], message="wip", author=AUTHOR
    )
    assert b"<<<<<<<" in controller.show(wip, "src/app.py")


async def test_merge_parents_reach_the_agent_commit(paths: TaskPaths, controller: Repo) -> None:
    branch, main = _branch_and_main(
        controller, "src/app.py=print('b')\n", "src/app.py=print('m')\n"
    )
    attempt = await merge_trees(controller.root, branch, main)
    wip = await commit_tree(
        controller.root, tree=attempt.tree, parents=[branch], message="wip", author=AUTHOR
    )
    await checkout_commit(controller.root, wip)
    agent = agent_repo(paths)
    agent.write("src/app.py", "print('b')\nprint('m')\n")

    merged = await apply_agent_patch(
        controller.root,
        patch=agent_patch(paths, wip),
        base_sha=wip,
        parent_sha=branch,
        extra_parents=[main],
        message="merge",
        author=AUTHOR,
    )

    parents = controller.git("rev-list", "--parents", "-n", "1", merged).decode().split()[1:]
    assert parents == [branch, main]
    assert controller.show(merged, "src/app.py") == b"print('b')\nprint('m')\n"


async def test_binary_diff_holds_the_whole_change(controller: Repo) -> None:
    base = controller.head()
    controller.write("bin.dat", b"\x00\x01")
    head = controller.commit()

    diff = await binary_diff(controller.root, base, head)

    assert b"GIT binary patch" in diff
    assert b"diff --git a/bin.dat b/bin.dat" in diff


@pytest.mark.parametrize("sha", ["main", "HEAD", "--upload-pack=x"])
async def test_new_git_helpers_require_full_shas(controller: Repo, sha: str) -> None:
    head = controller.head()
    with pytest.raises(ValueError, match="sha"):
        await merge_trees(controller.root, head, sha)
    with pytest.raises(ValueError, match="sha"):
        await checkout_commit(controller.root, sha)
    with pytest.raises(ValueError, match="sha"):
        await commit_tree(controller.root, tree=sha, parents=[head], message="m", author=AUTHOR)
