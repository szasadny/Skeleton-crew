import ast
from pathlib import Path

import skeleton_crew

SRC_DIR = Path(skeleton_crew.__file__).parent

# May only shrink: a new module-level import cycle is a design error, not an allowlist entry.
ALLOWED_CYCLES: frozenset[frozenset[str]] = frozenset()


def module_name(path: Path) -> str:
    parts = path.relative_to(SRC_DIR.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _package(path: Path) -> str:
    name = module_name(path)
    return name if path.name == "__init__.py" else name.rpartition(".")[0]


def _resolve_from(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    anchor = package.rsplit(".", node.level - 1)[0]
    return f"{anchor}.{node.module}" if node.module else anchor


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_time_imports(body: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    for node in body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.append(node)
        elif isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                found += _import_time_imports(node.body)
            found += _import_time_imports(node.orelse)
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody):
                found += _import_time_imports(block)
            for handler in node.handlers:
                found += _import_time_imports(handler.body)
    return found


def _targets(node: ast.Import | ast.ImportFrom, package: str, sources: set[str]) -> set[str]:
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            module = alias.name
            while module and module not in sources:
                module = module.rpartition(".")[0]
            targets.add(module)
        return targets
    base = _resolve_from(node, package)
    for alias in node.names:
        submodule = f"{base}.{alias.name}"
        targets.add(submodule if submodule in sources else base)
    return targets


def import_graph() -> dict[str, set[str]]:
    paths = {module_name(path): path for path in SRC_DIR.rglob("*.py")}
    sources = set(paths)
    graph: dict[str, set[str]] = {name: set() for name in paths}
    for name, path in paths.items():
        package = _package(path)
        for node in _import_time_imports(ast.parse(path.read_text(encoding="utf-8")).body):
            graph[name] |= {
                target
                for target in _targets(node, package, sources)
                if target in sources and target != name
            }
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    todo = list(graph[start])
    while todo:
        node = todo.pop()
        if node not in seen:
            seen.add(node)
            todo.extend(graph[node])
    return seen


def cycles(graph: dict[str, set[str]]) -> set[frozenset[str]]:
    reach = {node: _reachable(graph, node) for node in graph}
    return {
        frozenset(other for other in reach[node] if node in reach[other]) | {node}
        for node in graph
        if node in reach[node]
    }


def test_cycle_detection_finds_a_cycle() -> None:
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}
    assert cycles(graph) == {frozenset({"a", "b", "c"})}


def test_no_new_module_level_import_cycles() -> None:
    found = cycles(import_graph())
    new = [sorted(cycle) for cycle in found - ALLOWED_CYCLES]
    resolved = [sorted(cycle) for cycle in ALLOWED_CYCLES - found]
    assert not new, f"new import cycle: {new}"
    assert not resolved, f"cycle resolved, remove it from ALLOWED_CYCLES: {resolved}"
