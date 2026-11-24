import ast
import io
import tokenize
from pathlib import Path

import skeleton_crew

SRC_DIR = Path(skeleton_crew.__file__).parent
REPO_DIR = SRC_DIR.parents[1]
TESTS_DIR = REPO_DIR / "tests"
MAX_COMMENT_BLOCK_LINES = 1
_NON_CODE_TOKENS = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
}
_DOCUMENTABLE = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def python_sources() -> list[Path]:
    return sorted(path for root in (SRC_DIR, TESTS_DIR) for path in root.rglob("*.py"))


def relative(path: Path) -> str:
    return path.relative_to(REPO_DIR).as_posix()


def _comment_blocks(source: str) -> list[list[int]]:
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    code_lines = {
        line
        for token in tokens
        if token.type not in _NON_CODE_TOKENS
        for line in range(token.start[0], token.end[0] + 1)
    }
    blocks: list[list[int]] = []
    for token in tokens:
        line = token.start[0]
        if token.type != tokenize.COMMENT or line in code_lines:
            continue
        if blocks and blocks[-1][-1] == line - 1:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return blocks


def test_comment_block_detection() -> None:
    source = "# one\n# two\nx = 1  # trailing\n# three\ny = 2\n"
    assert _comment_blocks(source) == [[1, 2], [4]]


def test_comment_blocks_stay_one_line() -> None:
    offenders = [
        f"{relative(path)}:{block[0]}"
        for path in python_sources()
        for block in _comment_blocks(path.read_text(encoding="utf-8"))
        if len(block) > MAX_COMMENT_BLOCK_LINES
    ]
    assert not offenders, (
        f"comment longer than one line; shorten it or put it in a test: {offenders}"
    )


def test_no_docstrings() -> None:
    offenders = [
        f"{relative(path)}:{getattr(node, 'lineno', 1)}"
        for path in python_sources()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, _DOCUMENTABLE) and ast.get_docstring(node) is not None
    ]
    assert not offenders, f"docstring found; the name and the tests document the code: {offenders}"
