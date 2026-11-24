import os
from pathlib import Path

import pytest
from gitrepo import GitRepo

from skeleton_crew.domain.diff_parse import ChangeStatus, FileChange, parse_diff_summary
from skeleton_crew.domain.diff_policy import check_diff
from skeleton_crew.domain.errors import Violation

PROTECTED = (".github/**", ".gitmodules")
SHA_A = "0" * 7
SHA_B = "bdc955b"


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path)


def text_change(
    path: str, added: int = 1, removed: int = 0, new_mode: str = "100644"
) -> FileChange:
    return FileChange(path, ChangeStatus.MODIFIED, "100644", new_mode, added, removed)


def summary(repo: GitRepo, base: str, head: str) -> list[FileChange]:
    return parse_diff_summary(repo.raw(base, head), repo.numstat(base, head))


def test_real_git_output_round_trips(repo: GitRepo) -> None:
    repo.write("keep.txt", "a\nb\n")
    repo.write("gone.txt", "x\n")
    repo.write("swap", "file\n")
    base = repo.commit()
    repo.write("keep.txt", "a\nB\nc\n")
    (repo.root / "gone.txt").unlink()
    (repo.root / "swap").unlink()
    os.symlink("keep.txt", repo.root / "swap")
    os.chmod(repo.root / "keep.txt", 0o755)  # noqa: S103
    repo.write("img.bin", b"\x00\x01\x02")
    repo.write("dir with space/ümläut.txt", "u\n")
    repo.write("new\nline.txt", "n\n")
    head = repo.commit()
    changes = {c.path: c for c in summary(repo, base, head)}
    assert changes["keep.txt"] == FileChange(
        "keep.txt", ChangeStatus.MODIFIED, "100644", "100755", 2, 1
    )
    assert changes["gone.txt"].status is ChangeStatus.DELETED
    assert changes["swap"].status is ChangeStatus.TYPE_CHANGED
    assert changes["swap"].new_mode == "120000"
    assert changes["img.bin"].added is None
    assert changes["dir with space/ümläut.txt"].added == 1
    assert changes["new\nline.txt"].status is ChangeStatus.ADDED
    violations = check_diff(list(changes.values()), PROTECTED, 1000)
    assert {v.path for v in violations} == {"swap", "new\nline.txt"}


def test_real_git_submodule_is_rejected(repo: GitRepo) -> None:
    base = repo.commit()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},vendor/lib")
    repo.git("commit", "-q", "-m", "sub")
    head = repo.git("rev-parse", "HEAD").strip()
    changes = summary(repo, base, head)
    assert changes[0].new_mode == "160000"
    assert check_diff(changes, (), 1000) == [Violation("vendor/lib", None, "submodule not allowed")]


def test_real_git_workflow_edit_is_protected(repo: GitRepo) -> None:
    repo.write("README", "r\n")
    base = repo.commit()
    repo.write(".github/workflows/ci.yml", "on: push\n")
    head = repo.commit()
    assert check_diff(summary(repo, base, head), PROTECTED, 1000) == [
        Violation(".github/workflows/ci.yml", None, "protected path")
    ]


def test_clean_text_change_passes() -> None:
    assert check_diff([text_change("src/app.py", 10, 5)], PROTECTED, 100) == []


def test_empty_change_list_is_a_violation() -> None:
    assert check_diff([], PROTECTED, 100) == [Violation("*", None, "no changes")]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        ".github",
        ".GITHUB/workflows/ci.yml",
        ".GitHub/dependabot.yml",
        ".gitmodules",
        ".GITMODULES",
        ".github/a/b/c/d.yml",
    ],
)
def test_protected_paths_are_violations(path: str) -> None:
    assert check_diff([text_change(path)], PROTECTED, 100) == [
        Violation(path, None, "protected path")
    ]


@pytest.mark.parametrize(
    "path",
    [
        "docs/.github/notes.md",
        ".githubx/ci.yml",
        "x.github/ci.yml",
        "src/.gitmodules.bak",
        ".gitignore",
        ".gitattributes",
        "github/workflows/ci.yml",
    ],
)
def test_lookalike_paths_outside_protected_patterns_pass(path: str) -> None:
    assert check_diff([text_change(path)], PROTECTED, 100) == []


@pytest.mark.parametrize(
    ("pattern", "path", "protected"),
    [
        ("**/secrets/**", "a/b/secrets/key", True),
        ("**/secrets/**", "secrets/key", True),
        ("**/secrets/**", "secrets", True),
        ("*.lock", "uv.lock", True),
        ("*.lock", "sub/uv.lock", False),
        ("**/*.lock", "sub/uv.lock", True),
        ("infra/*", "infra/main.tf", True),
        ("infra/*", "infra/mod/main.tf", False),
        ("infra/**", "infra/mod/main.tf", True),
        ("infra/**", "infra", True),
        ("infra/**", "infrastructure/x", False),
    ],
)
def test_protected_pattern_semantics(pattern: str, path: str, protected: bool) -> None:
    violations = check_diff([text_change(path)], (pattern,), 100)
    assert (violations == [Violation(path, None, "protected path")]) is protected


def test_deleting_a_protected_file_is_a_violation() -> None:
    change = FileChange(".github/workflows/ci.yml", ChangeStatus.DELETED, "100644", "000000", 0, 9)
    assert check_diff([change], PROTECTED, 100)[0].reason == "protected path"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside",
        "a/../../b",
        "a/..",
        "..",
        ".git/config",
        ".GIT/config",
        ".Git/hooks/pre-commit",
        "sub/.git/config",
        "sub/.git",
        ".git",
        ".git./config",
        ".git /config",
        ".git::$INDEX_ALLOCATION/config",
        "GIT~1/config",
        "a\x00b",
        "a\nb",
        "a\rb",
        "a\x1bb",
        "a\x7fb",
        "a\u200b/.git/config",
        ".g\u200bit/config",
        "evil\u202etxt.exe",
        "a\\..\\b",
        "a//b",
        "a/./b",
        "./a",
        "a/",
        "",
    ],
)
def test_unsafe_paths_are_violations(path: str) -> None:
    violations = check_diff([text_change(path)], PROTECTED, 100)
    assert len(violations) == 1
    assert violations[0].path == path
    assert violations[0].reason != "protected path"


@pytest.mark.parametrize("path", [".github/../.git/config", ".github/./x"])
def test_unsafe_path_is_reported_even_inside_protected_dir(path: str) -> None:
    assert check_diff([text_change(path)], PROTECTED, 100)[0].reason != "protected path"


@pytest.mark.parametrize(
    ("status", "old_mode", "new_mode", "reason"),
    [
        (ChangeStatus.ADDED, "000000", "120000", "symlink not allowed"),
        (ChangeStatus.TYPE_CHANGED, "100644", "120000", "symlink not allowed"),
        (ChangeStatus.MODIFIED, "120000", "120000", "symlink not allowed"),
        (ChangeStatus.ADDED, "000000", "160000", "submodule not allowed"),
        (ChangeStatus.TYPE_CHANGED, "100644", "160000", "submodule not allowed"),
    ],
)
def test_symlinks_and_submodules_are_violations(
    status: ChangeStatus, old_mode: str, new_mode: str, reason: str
) -> None:
    change = FileChange("link", status, old_mode, new_mode, 1, 0)
    assert check_diff([change], PROTECTED, 100) == [Violation("link", None, reason)]


@pytest.mark.parametrize(
    ("status", "old_mode", "new_mode"),
    [
        (ChangeStatus.MODIFIED, "100644", "100755"),
        (ChangeStatus.MODIFIED, "100755", "100644"),
        (ChangeStatus.TYPE_CHANGED, "120000", "100644"),
        (ChangeStatus.DELETED, "120000", "000000"),
        (ChangeStatus.DELETED, "160000", "000000"),
    ],
)
def test_allowed_mode_changes_pass(status: ChangeStatus, old_mode: str, new_mode: str) -> None:
    change = FileChange("f", status, old_mode, new_mode, 0, 1)
    assert check_diff([change], PROTECTED, 100) == []


def test_symlink_into_protected_path_reports_both() -> None:
    change = FileChange(".github/x", ChangeStatus.ADDED, "000000", "120000", 1, 0)
    reasons = {v.reason for v in check_diff([change], PROTECTED, 100)}
    assert reasons == {"protected path", "symlink not allowed"}


@pytest.mark.parametrize(
    ("counts", "violates"),
    [
        ([(50, 50)], False),
        ([(50, 51)], True),
        ([(100, 0)], False),
        ([(0, 101)], True),
        ([(60, 0), (41, 0)], True),
        ([(60, 0), (40, 0)], False),
    ],
)
def test_size_limit_counts_added_and_removed(counts: list[tuple[int, int]], violates: bool) -> None:
    changes = [text_change(f"f{i}", a, r) for i, (a, r) in enumerate(counts)]
    violations = check_diff(changes, PROTECTED, 100)
    assert any(v.path == "*" for v in violations) is violates


def test_binary_files_do_not_count_towards_size() -> None:
    changes = [
        FileChange("big.bin", ChangeStatus.ADDED, "000000", "100644", None, None),
        text_change("a.txt", 100, 0),
    ]
    assert check_diff(changes, PROTECTED, 100) == []


def test_all_violations_are_reported_together() -> None:
    changes = [
        text_change(".github/ci.yml", 500, 0),
        text_change("../x"),
        FileChange("l", ChangeStatus.ADDED, "000000", "120000", 1, 0),
    ]
    paths = [v.path for v in check_diff(changes, PROTECTED, 100)]
    assert paths == [".github/ci.yml", "../x", "l", "*"]


def test_rendered_violations_are_pinned() -> None:
    changes = [
        text_change(".github/ci.yml", 500, 0),
        text_change("../x"),
        FileChange("l", ChangeStatus.ADDED, "000000", "120000", 1, 0),
        FileChange("vendor", ChangeStatus.ADDED, "000000", "160000", 1, 0),
    ]
    rendered = [str(v) for v in check_diff(changes, PROTECTED, 100)]
    assert rendered == [
        ".github/ci.yml: protected path",
        "../x: '..' in path",
        "l: symlink not allowed",
        "vendor: submodule not allowed",
        "*: diff has 503 lines, limit is 100",
    ]
    assert [str(v) for v in check_diff([], PROTECTED, 100)] == ["*: no changes"]
