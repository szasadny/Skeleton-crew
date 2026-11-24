import os
from pathlib import Path

import pytest
from gitrepo import GitRepo

from skeleton_crew.domain.diff_parse import (
    AddedLine,
    ChangeStatus,
    FileChange,
    added_lines,
    parse_diff_summary,
)
from skeleton_crew.domain.errors import PolicyViolation


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path)


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


def test_parses_verified_raw_and_numstat_output() -> None:
    assert parse_diff_summary(VERIFIED_RAW, VERIFIED_NUMSTAT) == [
        FileChange("bin.dat", ChangeStatus.ADDED, "000000", "100644", None, None),
        FileChange("del.txt", ChangeStatus.DELETED, "100644", "000000", 0, 1),
        FileChange("m.txt", ChangeStatus.MODIFIED, "100644", "100755", 2, 1),
        FileChange("newlink", ChangeStatus.ADDED, "000000", "120000", 1, 0),
        FileChange("sp ace/é f.txt", ChangeStatus.ADDED, "000000", "100644", 1, 0),
        FileChange("tab\tname", ChangeStatus.ADDED, "000000", "100644", 1, 0),
        FileChange("tc.txt", ChangeStatus.TYPE_CHANGED, "100644", "120000", 1, 1),
    ]


def test_real_git_renames_are_split_into_delete_and_add(repo: GitRepo) -> None:
    repo.write("old.txt", "same content\n" * 5)
    base = repo.commit()
    (repo.root / "old.txt").rename(repo.root / "new.txt")
    head = repo.commit()
    statuses = {c.path: c.status for c in summary(repo, base, head)}
    assert statuses == {"old.txt": ChangeStatus.DELETED, "new.txt": ChangeStatus.ADDED}


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


VERIFIED_DIFF = """diff --git a/bin.dat b/bin.dat
new file mode 100644
index 0000000..bdc955b
Binary files /dev/null and b/bin.dat differ
diff --git a/del.txt b/del.txt
deleted file mode 100644
index c1b0730..0000000
--- a/del.txt
+++ /dev/null
@@ -1 +0,0 @@
-x
\\ No newline at end of file
diff --git a/m.txt b/m.txt
old mode 100644
new mode 100755
index 422c2b7..7be73ce
--- a/m.txt
+++ b/m.txt
@@ -2 +2,2 @@ a
-b
+B
+c
diff --git a/newlink b/newlink
new file mode 120000
index 0000000..3594e94
--- /dev/null
+++ b/newlink
@@ -0,0 +1 @@
+/etc/passwd
\\ No newline at end of file
diff --git "a/sp ace/\\303\\251 f.txt" "b/sp ace/\\303\\251 f.txt"
new file mode 100644
index 0000000..bca70f3
--- /dev/null
+++ "b/sp ace/\\303\\251 f.txt"\t
@@ -0,0 +1 @@
+q
diff --git "a/tab\\tname" "b/tab\\tname"
new file mode 100644
index 0000000..b680253
--- /dev/null
+++ "b/tab\\tname"
@@ -0,0 +1 @@
+z
diff --git a/a b.txt b/a b.txt
new file mode 100644
index 0000000..b68fde2
--- /dev/null
+++ b/a b.txt\t
@@ -0,0 +1 @@
+k
diff --git a/plus.txt b/plus.txt
new file mode 100644
index 0000000..36749ef
--- /dev/null
+++ b/plus.txt
@@ -0,0 +1 @@
++++ x
"""


def test_parses_verified_unified_diff() -> None:
    assert added_lines(VERIFIED_DIFF) == [
        AddedLine("m.txt", 2, "B"),
        AddedLine("m.txt", 3, "c"),
        AddedLine("newlink", 1, "/etc/passwd"),
        AddedLine("sp ace/é f.txt", 1, "q"),
        AddedLine("tab\tname", 1, "z"),
        AddedLine("a b.txt", 1, "k"),
        AddedLine("plus.txt", 1, "+++ x"),
    ]


def test_real_git_diff_line_numbers(repo: GitRepo) -> None:
    repo.write("a.py", "".join(f"line{i}\n" for i in range(1, 11)))
    base = repo.commit()
    content = [f"line{i}\n" for i in range(1, 11)]
    content[2] = "changed3\n"
    content.insert(6, "inserted\n")
    content.append("+++ b/fake\n")
    content.append("@@ -1 +1 @@\n")
    repo.write("a.py", "".join(content))
    repo.write("dir with space/ü.txt", "x\n")
    repo.write('q"uote.txt', "y")
    head = repo.commit()
    assert added_lines(repo.unified(base, head)) == [
        AddedLine("a.py", 3, "changed3"),
        AddedLine("a.py", 7, "inserted"),
        AddedLine("a.py", 12, "+++ b/fake"),
        AddedLine("a.py", 13, "@@ -1 +1 @@"),
        AddedLine("dir with space/ü.txt", 1, "x"),
        AddedLine('q"uote.txt', 1, "y"),
    ]


def test_real_git_deletions_and_type_changes(repo: GitRepo) -> None:
    repo.write("gone.txt", "a\nb\n")
    repo.write("swap", "file\n")
    base = repo.commit()
    (repo.root / "gone.txt").unlink()
    (repo.root / "swap").unlink()
    os.symlink("target", repo.root / "swap")
    head = repo.commit()
    assert added_lines(repo.unified(base, head)) == [AddedLine("swap", 1, "target")]


def test_crlf_lines_keep_carriage_return(repo: GitRepo) -> None:
    base = repo.commit()
    repo.write("w.txt", "a\r\nb\r\n")
    head = repo.commit()
    assert [line.text for line in added_lines(repo.unified(base, head))] == ["a\r", "b\r"]


def test_empty_diff_has_no_added_lines() -> None:
    assert added_lines("") == []


@pytest.mark.parametrize(
    "diff",
    [
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n+only one\n",
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ bogus @@\n+a\n",
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n+a\n+extra\n",
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n?a\n",
        "diff --git a/x b/x\n@@ -0,0 +1 @@\n+no file header\n",
        "diff --git a/x b/x\n--- a/x\n+++ x\n@@ -0,0 +1 @@\n+a\n",
        "diff --git a/x b/x\n--- a/x\n+++ b/\n@@ -0,0 +1 @@\n+a\n",
        'diff --git a/x b/x\n--- a/x\n+++ "b/unterminated\n@@ -0,0 +1 @@\n+a\n',
        'diff --git a/x b/x\n--- a/x\n+++ "b/bad\\q"\n@@ -0,0 +1 @@\n+a\n',
        'diff --git a/x b/x\n--- a/x\n+++ "b/\\377"\n@@ -0,0 +1 @@\n+a\n',
        "+stray added line\n",
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n+c\n",
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n context\n context\n",
    ],
)
def test_malformed_diff_fails_closed(diff: str) -> None:
    with pytest.raises(PolicyViolation):
        added_lines(diff)


def test_no_newline_marker_is_ignored() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
    assert added_lines(diff) == [AddedLine("x", 1, "b")]
