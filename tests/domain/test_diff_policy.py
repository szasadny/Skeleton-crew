import os
from pathlib import Path

import pytest
from gitrepo import GitRepo

from skeleton_crew.domain.diff_policy import (
    ChangeStatus,
    DiffViolation,
    FileChange,
    check_diff,
    parse_diff_summary,
)
from skeleton_crew.domain.errors import PolicyViolation

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


VERIFIED_RAW = (
    ":000000 100644 0000000 bdc955b A\0bin.dat\0"
    ":100644 000000 c1b0730 0000000 D\0del.txt\0"
    ":100644 100755 422c2b7 7be73ce M\0m.txt\0"
    ":000000 120000 0000000 3594e94 A\0newlink\0"
    ":000000 100644 0000000 bca70f3 A\0sp ace/é f.txt\0"
    ":000000 100644 0000000 b680253 A\0tab\tname\0"
    ":100644 120000 975fbec 347bda8 T\0tc.txt\0"
)
VERIFIED_NUMSTAT = (
    "-\t-\tbin.dat\0"
    "0\t1\tdel.txt\0"
    "2\t1\tm.txt\0"
    "1\t0\tnewlink\0"
    "1\t0\tsp ace/é f.txt\0"
    "1\t0\ttab\tname\0"
    "1\t1\ttc.txt\0"
)


def test_parses_verified_git_output() -> None:
    assert parse_diff_summary(VERIFIED_RAW, VERIFIED_NUMSTAT) == [
        FileChange("bin.dat", ChangeStatus.ADDED, "000000", "100644", None, None),
        FileChange("del.txt", ChangeStatus.DELETED, "100644", "000000", 0, 1),
        FileChange("m.txt", ChangeStatus.MODIFIED, "100644", "100755", 2, 1),
        FileChange("newlink", ChangeStatus.ADDED, "000000", "120000", 1, 0),
        FileChange("sp ace/é f.txt", ChangeStatus.ADDED, "000000", "100644", 1, 0),
        FileChange("tab\tname", ChangeStatus.ADDED, "000000", "100644", 1, 0),
        FileChange("tc.txt", ChangeStatus.TYPE_CHANGED, "100644", "120000", 1, 1),
    ]


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


def test_real_git_renames_are_split_into_delete_and_add(repo: GitRepo) -> None:
    repo.write("old.txt", "same content\n" * 5)
    base = repo.commit()
    (repo.root / "old.txt").rename(repo.root / "new.txt")
    head = repo.commit()
    statuses = {c.path: c.status for c in summary(repo, base, head)}
    assert statuses == {"old.txt": ChangeStatus.DELETED, "new.txt": ChangeStatus.ADDED}


def test_real_git_submodule_is_rejected(repo: GitRepo) -> None:
    base = repo.commit()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},vendor/lib")
    repo.git("commit", "-q", "-m", "sub")
    head = repo.git("rev-parse", "HEAD").strip()
    changes = summary(repo, base, head)
    assert changes[0].new_mode == "160000"
    assert check_diff(changes, (), 1000) == [DiffViolation("vendor/lib", "submodule not allowed")]


def test_real_git_workflow_edit_is_protected(repo: GitRepo) -> None:
    repo.write("README", "r\n")
    base = repo.commit()
    repo.write(".github/workflows/ci.yml", "on: push\n")
    head = repo.commit()
    assert check_diff(summary(repo, base, head), PROTECTED, 1000) == [
        DiffViolation(".github/workflows/ci.yml", "protected path")
    ]


def test_empty_outputs_parse_to_no_changes() -> None:
    assert parse_diff_summary("", "") == []


@pytest.mark.parametrize(
    "raw",
    [
        ":100644 100644 422c2b7 7be73ce R100\0a\0b\0",
        ":100644 100644 422c2b7 7be73ce C075\0a\0b\0",
        ":100644 100644 422c2b7 7be73ce U\0a\0",
        ":100644 100644 422c2b7 7be73ce X\0a\0",
        ":100644 100644 422c2b7 7be73ce m\0a\0",
        ":100644 100644 422c2b7 7be73ce MM\0a\0",
        "::100644 100644 100644 422c2b7 422c2b7 7be73ce MM\0a\0",
        ":100644 100644 422c2b7 7be73ce M\0a",
        ":100644 100644 422c2b7 7be73ce M\0",
        ":100644 100644 422c2b7 7be73ce M\0\0",
        ":100644 100644 422c2b7 7be73ce M\ta\0",
        "100644 100644 422c2b7 7be73ce M\0a\0",
        ":100644 100644 422c2b 7be73ce M\0a\0",
        ":100644 100644 422c2bZ 7be73ce M\0a\0",
        ":100644 100644 422C2B7 7be73ce M\0a\0",
        ":100644  100644 422c2b7 7be73ce M\0a\0",
        ":100644 100644 422c2b7 7be73ce M \0a\0",
        ":100644 040000 422c2b7 7be73ce T\0a\0",
        ":100644 100666 422c2b7 7be73ce M\0a\0",
        ":100644 1006444 422c2b7 7be73ce M\0a\0",
        ":000000 100644 422c2b7 7be73ce M\0a\0",
        ":100644 000000 422c2b7 7be73ce M\0a\0",
        ":100644 100644 422c2b7 7be73ce A\0a\0",
        ":000000 000000 422c2b7 7be73ce D\0a\0",
        ":100644 100644 422c2b7 7be73ce D\0a\0",
        "\uff1a100644 100644 422c2b7 7be73ce M\0a\0",
        ":\u0661\u0660\u0660\u0666\u0664\u0664 100644 422c2b7 7be73ce M\0a\0",
    ],
)
def test_unparseable_raw_output_fails_closed(raw: str) -> None:
    with pytest.raises(PolicyViolation):
        parse_diff_summary(raw, "1\t0\ta\0")


@pytest.mark.parametrize(
    "numstat",
    [
        "",
        "1\t0\ta",
        "1\t0\tb\0",
        "1\t0\t\0a\0b\0",
        "1 0 a\0",
        "1\t0\0",
        "x\t0\ta\0",
        "-\t0\ta\0",
        "0\t-\ta\0",
        "-1\t0\ta\0",
        "01\t0\ta\0",
        "1\t0\ta\0" * 2,
        "\u0661\t0\ta\0",
        "1\t0\ta\0extra",
        "12345678901\t0\ta\0",
    ],
)
def test_unparseable_or_mismatched_numstat_fails_closed(numstat: str) -> None:
    with pytest.raises(PolicyViolation):
        parse_diff_summary(":100644 100644 422c2b7 7be73ce M\0a\0", numstat)


def test_duplicate_paths_fail_closed() -> None:
    raw = ":100644 100644 422c2b7 7be73ce M\0a\0" * 2
    with pytest.raises(PolicyViolation, match="duplicate"):
        parse_diff_summary(raw, "1\t0\ta\0" * 2)


def test_full_length_shas_are_accepted() -> None:
    raw = f":100644 100644 {'a' * 40} {'b' * 64} M\0a\0"
    assert parse_diff_summary(raw, "1\t1\ta\0")[0].added == 1


def test_numstat_order_must_match_raw_order() -> None:
    raw = ":100644 100644 422c2b7 7be73ce M\0a\0:100644 100644 422c2b7 7be73ce M\0b\0"
    with pytest.raises(PolicyViolation):
        parse_diff_summary(raw, "1\t0\tb\0" + "1\t0\ta\0")


def test_clean_text_change_passes() -> None:
    assert check_diff([text_change("src/app.py", 10, 5)], PROTECTED, 100) == []


def test_empty_change_list_is_a_violation() -> None:
    assert check_diff([], PROTECTED, 100) == [DiffViolation("*", "no changes")]


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
        DiffViolation(path, "protected path")
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
    assert (violations == [DiffViolation(path, "protected path")]) is protected


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
    assert check_diff([change], PROTECTED, 100) == [DiffViolation("link", reason)]


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
