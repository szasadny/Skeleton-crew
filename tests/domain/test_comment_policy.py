import os
from pathlib import Path

import pytest
from gitrepo import GitRepo

from skeleton_crew.domain.comment_policy import (
    CommentFlag,
    comment_block_findings,
    describe_comment_blocks,
    flag_long_comment_blocks,
)
from skeleton_crew.domain.contest_policy import CheckId, CheckInput, Finding
from skeleton_crew.domain.diff_lines import AddedLine, added_lines
from skeleton_crew.domain.errors import PolicyViolation
from skeleton_crew.domain.settings import AppSettings, Limits


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return GitRepo(tmp_path)


def lines(path: str, *texts: str, start: int = 1) -> list[AddedLine]:
    return [AddedLine(path, start + i, t) for i, t in enumerate(texts)]


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


def test_parses_verified_git_output() -> None:
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


@pytest.mark.parametrize(
    "text",
    [
        "# python",
        "  # indented",
        "// c",
        "-- sql",
        "; lisp",
        "/* c block */",
        "* javadoc",
        "*/",
        "<!-- html -->",
        '"""doc"""',
        "'''doc'''",
        "% tex",
        "rem batch",
        "REM batch",
        "Rem batch",
        "#endif",
        "# include not a directive",
    ],
)
def test_comment_lines_are_flagged_above_limit(text: str) -> None:
    flags = flag_long_comment_blocks(lines("f", text, text, text, start=5), 2)
    assert flags == [CommentFlag("f", 5, 3)]


@pytest.mark.parametrize(
    "text",
    [
        "code()",
        "x = 1  # trailing",
        "#include <stdio.h>",
        "#define X 1",
        "#if FOO",
        "#ifdef FOO",
        "#ifndef FOO",
        "#pragma once",
        "#region Main",
        "",
        "   ",
        "remove()",
        "rem",
        "return x",
        "@decorator",
    ],
)
def test_non_comment_lines_are_not_flagged(text: str) -> None:
    assert flag_long_comment_blocks(lines("f", text, text, text, text), 2) == []


def test_block_at_limit_is_not_flagged() -> None:
    assert flag_long_comment_blocks(lines("f", "# a", "# b"), 2) == []


def test_shebang_on_first_line_is_not_a_comment() -> None:
    assert flag_long_comment_blocks(lines("f", "#!/usr/bin/env python", "# a", "# b"), 2) == []


def test_shebang_after_first_line_is_a_comment() -> None:
    flags = flag_long_comment_blocks(lines("f", "#!x", "# a", "# b", start=2), 2)
    assert flags == [CommentFlag("f", 2, 3)]


def test_gap_in_line_numbers_splits_blocks() -> None:
    added = [*lines("f", "# a", "# b"), *lines("f", "# c", "# d", start=4)]
    assert flag_long_comment_blocks(added, 2) == []


def test_code_line_splits_blocks() -> None:
    assert flag_long_comment_blocks(lines("f", "# a", "# b", "x = 1", "# c", "# d"), 2) == []


def test_blocks_do_not_span_files() -> None:
    added = [*lines("a", "# 1", "# 2"), *lines("b", "# 3", start=3)]
    assert flag_long_comment_blocks(added, 2) == []


def test_multiple_long_blocks_are_all_flagged() -> None:
    added = [*lines("a", "#", "#", "#"), *lines("b", "//", "//", "//", "//", start=9)]
    assert flag_long_comment_blocks(added, 2) == [CommentFlag("a", 1, 3), CommentFlag("b", 9, 4)]


@pytest.mark.parametrize(
    ("opener", "middle", "closer"),
    [
        ('"""Summary.', "plain prose line", '"""'),
        ("'''Summary.", "plain prose line", "'''"),
        ("/*", "plain prose line", "*/"),
        ("<!--", "plain prose line", "-->"),
        ('    """', "        indented prose", '    """'),
    ],
)
def test_multiline_block_comment_counts_every_line(opener: str, middle: str, closer: str) -> None:
    added = lines("f", "def f():", opener, middle, middle, closer, "    return 1")
    assert flag_long_comment_blocks(added, 3) == [CommentFlag("f", 2, 4)]


def test_one_line_docstring_does_not_swallow_following_code() -> None:
    added = lines("f", '"""One line."""', "x = 1", "y = 2", "z = 3")
    assert flag_long_comment_blocks(added, 1) == []


def test_unterminated_docstring_ends_at_gap() -> None:
    added = [*lines("f", '"""start', "prose"), *lines("f", "x = 1", "y = 2", start=10)]
    assert flag_long_comment_blocks(added, 1) == [CommentFlag("f", 1, 2)]


def test_docstring_closing_mid_line_ends_block() -> None:
    added = lines("f", '"""start', 'end."""', "code = 1", "more = 2")
    assert flag_long_comment_blocks(added, 1) == [CommentFlag("f", 1, 2)]


def test_end_to_end_from_diff_text() -> None:
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -3,0 +4,4 @@\n"
        "+# one\n+# two\n+# three\n+code()\n"
    )
    assert flag_long_comment_blocks(added_lines(diff), 2) == [CommentFlag("x.py", 4, 3)]


@pytest.mark.parametrize("path", ["README.md", "docs/Guide.RST", "notes.txt"])
def test_prose_files_are_not_flagged(path: str) -> None:
    lines = [AddedLine(path, n, f"* bullet {n}") for n in range(1, 6)]
    assert flag_long_comment_blocks(lines, 2) == []


def test_comment_block_findings_use_the_configured_limit() -> None:
    settings = AppSettings(limits=Limits(max_code_comment_lines=2))
    added = lines("app.py", "# one", "# two", "# three", "code()")
    assert comment_block_findings(CheckInput([], added, settings)) == [
        Finding(CheckId.COMMENT_BLOCK, "app.py", 1, "comment block of 3 lines, limit is 2")
    ]
    assert describe_comment_blocks(settings) == "comment blocks longer than 2 lines"
