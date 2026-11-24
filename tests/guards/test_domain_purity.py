import ast
from pathlib import Path

import skeleton_crew.domain

DOMAIN_DIR = Path(skeleton_crew.domain.__file__).parent
# Only pure modules: nothing here reads a clock, the environment, a file or the network.
ALLOWED_IMPORTS = frozenset(
    {
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "fnmatch",
        "functools",
        "itertools",
        "json",
        "math",
        "pathlib",
        "posixpath",
        "pydantic",
        "re",
        "typing",
        "unicodedata",
        "zoneinfo",
    }
)
_OWN_PACKAGE = "skeleton_crew.domain"


def _imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level:
            modules.add(node.module or "")
    return modules


def _allowed(module: str) -> bool:
    if module == _OWN_PACKAGE or module.startswith(f"{_OWN_PACKAGE}."):
        return True
    return module.partition(".")[0] in ALLOWED_IMPORTS


def test_domain_imports_only_pure_modules() -> None:
    offenders = sorted(
        f"{path.name}: {module}"
        for path in DOMAIN_DIR.glob("*.py")
        for module in _imports(ast.parse(path.read_text(encoding="utf-8")))
        if not _allowed(module)
    )
    assert not offenders, f"domain/ must stay pure; move the I/O to another package: {offenders}"
