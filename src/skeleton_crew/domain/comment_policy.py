from collections.abc import Sequence
from dataclasses import dataclass

from skeleton_crew.domain.contest_policy import CheckId, CheckInput, Finding
from skeleton_crew.domain.diff_lines import AddedLine
from skeleton_crew.domain.settings import AppSettings


@dataclass(frozen=True)
class CommentFlag:
    path: str
    start_line: int
    length: int


_Blocks = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Syntax:
    line: tuple[str, ...]
    blocks: _Blocks = ()


_PY_DOC: _Blocks = (('"""', '"""'), ("'''", "'''"))
_C_BLOCK: _Blocks = (("/*", "*/"),)
_XML_BLOCK: _Blocks = (("<!--", "-->"),)
_HASH = _Syntax(("#",))
_C_LIKE = _Syntax(("//",), _C_BLOCK)
_UNKNOWN = _Syntax(("#", "//"), _PY_DOC + _C_BLOCK + _XML_BLOCK)

_SYNTAX_GROUPS: tuple[tuple[_Syntax, tuple[str, ...]], ...] = (
    (_Syntax(("#",), _PY_DOC), ("py", "pyi", "pyw")),
    (_HASH, ("sh", "bash", "zsh", "fish", "rb", "pl", "pm", "r", "ps1", "nix", "tf")),
    (_HASH, ("yaml", "yml", "toml", "cfg", "conf", "cmake", "mk", "dockerfile")),
    (_C_LIKE, ("c", "h", "cc", "cpp", "hpp", "cs", "java", "js", "jsx", "mjs", "cjs")),
    (_C_LIKE, ("ts", "tsx", "go", "rs", "swift", "kt", "kts", "scala", "dart", "groovy")),
    (_C_LIKE, ("gradle", "scss", "less")),
    (_Syntax(("//", "#"), _C_BLOCK), ("php",)),
    (_Syntax(("//",), _C_BLOCK + _XML_BLOCK), ("vue", "svelte")),
    (_Syntax((), _C_BLOCK), ("css",)),
    (_Syntax((), _XML_BLOCK), ("html", "htm", "xhtml", "xml", "svg")),
    (_Syntax(("--",), _C_BLOCK), ("sql",)),
    (_Syntax(("--",)), ("lua", "hs", "elm", "ada", "adb", "ads")),
    (_Syntax((";",)), ("lisp", "clj", "cljs", "el", "scm", "asm", "s")),
    (_Syntax((";", "#")), ("ini",)),
    (_Syntax(("%",)), ("tex", "erl", "m")),
    (_Syntax(("rem ", "::")), ("bat", "cmd")),
)
_SYNTAX_BY_EXTENSION = {ext: syntax for syntax, exts in _SYNTAX_GROUPS for ext in exts}
_SYNTAX_BY_NAME = dict.fromkeys(("dockerfile", "containerfile", "makefile", "gnumakefile"), _HASH)
_NOT_COMMENTS = ("#include", "#define", "#if", "#pragma", "#region")
_PROSE_SUFFIXES = (".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc")


def _syntax(path: str) -> _Syntax:
    name = path.rpartition("/")[2].casefold()
    if name in _SYNTAX_BY_NAME:
        return _SYNTAX_BY_NAME[name]
    stem, dot, extension = name.rpartition(".")
    return _SYNTAX_BY_EXTENSION.get(extension, _UNKNOWN) if dot and stem else _UNKNOWN


def _classify(line: AddedLine) -> tuple[bool, str | None]:
    syntax = _syntax(line.path)
    text = line.text.strip()
    for opener, closer in syntax.blocks:
        if text.startswith(opener):
            return True, None if closer in text[len(opener) :] else closer
    if line.line == 1 and text.startswith("#!"):
        return False, None
    if text.startswith(_NOT_COMMENTS):
        return False, None
    return text.casefold().startswith(syntax.line), None


def _comment_mask(lines: Sequence[AddedLine]) -> list[bool]:
    mask: list[bool] = []
    closer: str | None = None
    for index, line in enumerate(lines):
        if index and not _adjacent(lines[index - 1], line):
            closer = None
        if closer is None:
            is_comment, closer = _classify(line)
        else:
            is_comment = True
            if closer in line.text:
                closer = None
        mask.append(is_comment)
    return mask


def _adjacent(first: AddedLine, second: AddedLine) -> bool:
    return first.path == second.path and second.line == first.line + 1


def flag_long_comment_blocks(lines: Sequence[AddedLine], max_lines: int) -> list[CommentFlag]:
    lines = [line for line in lines if not line.path.casefold().endswith(_PROSE_SUFFIXES)]
    mask = _comment_mask(lines)
    flags: list[CommentFlag] = []
    start = 0
    while start < len(lines):
        if not mask[start]:
            start += 1
            continue
        end = start
        while end + 1 < len(lines) and mask[end + 1] and _adjacent(lines[end], lines[end + 1]):
            end += 1
        if end - start + 1 > max_lines:
            flags.append(CommentFlag(lines[start].path, lines[start].line, end - start + 1))
        start = end + 1
    return flags


def comment_block_findings(diff: CheckInput) -> list[Finding]:
    limit = diff.settings.limits.max_code_comment_lines
    return [
        Finding(
            CheckId.COMMENT_BLOCK,
            flag.path,
            flag.start_line,
            f"comment block of {flag.length} lines, limit is {limit}",
        )
        for flag in flag_long_comment_blocks(diff.lines, limit)
    ]


def describe_comment_blocks(settings: AppSettings) -> str:
    return f"comment blocks longer than {settings.limits.max_code_comment_lines} lines"
