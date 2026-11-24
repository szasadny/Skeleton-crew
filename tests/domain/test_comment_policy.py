import pytest

from skeleton_crew.domain.comment_policy import CommentFlag, flag_long_comment_blocks
from skeleton_crew.domain.diff_parse import AddedLine, added_lines


def lines(path: str, *texts: str, start: int = 1) -> list[AddedLine]:
    return [AddedLine(path, start + i, t) for i, t in enumerate(texts)]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("f", "# python"),
        ("f", "  # indented"),
        ("f", "// c"),
        ("q.sql", "-- sql"),
        ("x.lua", "-- lua"),
        ("x.lisp", "; lisp"),
        ("x.ini", "; ini"),
        ("f", "/* c block */"),
        ("x.html", "<!-- html -->"),
        ("x.py", '"""doc"""'),
        ("x.py", "'''doc'''"),
        ("x.tex", "% tex"),
        ("x.bat", "rem batch"),
        ("x.bat", "REM batch"),
        ("x.cmd", "Rem batch"),
        ("f", "#endif"),
        ("f", "# include not a directive"),
        ("Dockerfile", "# docker"),
        ("sub/Makefile", "# make"),
        ("x.php", "# php"),
        ("x.vue", "// vue"),
    ],
)
def test_comment_lines_are_flagged_above_limit(path: str, text: str) -> None:
    flags = flag_long_comment_blocks(lines(path, text, text, text, start=5), 2)
    assert flags == [CommentFlag(path, 5, 3)]


@pytest.mark.parametrize(
    ("path", "texts"),
    [
        ("run.sh", ("docker run \\", "  --rm \\", "  --network none \\", "  --read-only \\")),
        ("f", ("  --rm \\", "  --network none \\", "  --read-only \\")),
        ("swap.c", ("*out = a;", "*in = b;", "*tmp = c;")),
        ("f", ("*out = a;", "*in = b;", "*tmp = c;")),
        ("x.py", ("-- not sql", "; not lisp", "% not tex")),
        ("x.sql", ("# not sql", "// not sql", "; not sql")),
        ("x.c", ("# not c", "-- not c", "% not c")),
        ("x.py", ("rem = 1", "rem = 2", "rem = 3")),
        ("x.py", ("<!-- not python", "still not", "-->")),
        ("x.html", ("/* not html", "still not", "*/")),
    ],
)
def test_prefixes_of_other_languages_are_not_comments(path: str, texts: tuple[str, ...]) -> None:
    assert flag_long_comment_blocks(lines(path, *texts), 2) == []


@pytest.mark.parametrize(
    ("path", "texts"),
    [
        ("app.py", ("# one", "# two", "# three")),
        ("app.ts", ("// one", "// two", "// three")),
        ("q.sql", ("-- one", "-- two", "-- three")),
        ("APP.PY", ("# one", "# two", "# three")),
        ("x.Ts", ("// one", "// two", "// three")),
        ("notes.weird", ("# one", "# two", "# three")),
        ("archive.tar.zz", ("// one", "// two", "// three")),
        (".bashrc", ("# one", "# two", "# three")),
        ("x.", ("# one", "# two", "# three")),
        ("lib.c", ("/**", " * one", " * two", " */")),
        ("lib.ts", ("/*", "* one", "* two", "*/")),
        ("q.sql", ("/* one", "two", "three */")),
    ],
)
def test_real_comment_blocks_still_flag(path: str, texts: tuple[str, ...]) -> None:
    assert flag_long_comment_blocks(lines(path, *texts), 2) == [CommentFlag(path, 1, len(texts))]


def test_star_lines_without_open_block_are_code() -> None:
    assert flag_long_comment_blocks(lines("x.c", " * a", " * b", " * c"), 2) == []


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
