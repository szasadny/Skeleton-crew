import ast
from pathlib import Path

from skeleton_crew.core.db import Base
from skeleton_crew.models import __all__ as model_names

TESTS_DIR = Path(__file__).parents[1]
REPO_DIR = TESTS_DIR.parent
HELPER_DIRS = (TESTS_DIR / "support", TESTS_DIR / "fakes")
SEEDING_DIRS = (TESTS_DIR / "store",)
FACTORIES = TESTS_DIR / "support" / "factories.py"
STORE_SEEDERS = frozenset({"create_run", "add_worker_task", "add_refine_task"})
# Only ever remove entries: these test the ORM rows themselves (DB constraints, an in-memory row).
ORM_ALLOWED = frozenset({"tests/core/test_db.py", "tests/web/test_web_exports.py"})


def _orm_models() -> frozenset[str]:
    mapped = {mapper.class_.__name__ for mapper in Base.registry.mappers}
    assert mapped == set(model_names)
    return frozenset(mapped)


def _called_name(call: ast.Call) -> str | None:
    match call.func:
        case ast.Name(id=name) | ast.Attribute(attr=name):
            return name
    return None


def _seeding_calls(tree: ast.AST, models: frozenset[str]) -> list[str]:
    names = (_called_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call))
    return [name for name in names if name in STORE_SEEDERS or name in models]


def _relative(path: Path) -> str:
    return path.relative_to(REPO_DIR).as_posix()


def _test_sources() -> list[Path]:
    return sorted(TESTS_DIR.rglob("*.py"))


def _outside(path: Path, dirs: tuple[Path, ...]) -> bool:
    return not any(directory in path.parents for directory in dirs)


def _is_fixture(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(target, ast.Attribute) and target.attr == "fixture"


def _fixtures(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_is_fixture(d) for d in node.decorator_list)
    }


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_seeding_detector_fires() -> None:
    source = "runs_store.create_run(s)\nadd_refine_task(s)\nRun(state=1)\nseed_run(s)\n"
    assert _seeding_calls(ast.parse(source), frozenset({"Run"})) == [
        "create_run",
        "add_refine_task",
        "Run",
    ]


def test_rows_are_seeded_through_the_factories() -> None:
    models = _orm_models()
    found = {
        _relative(path): calls
        for path in _test_sources()
        if path != FACTORIES and _outside(path, SEEDING_DIRS)
        if (calls := _seeding_calls(_parse(path), models))
    }
    offenders = {path: calls for path, calls in found.items() if path not in ORM_ALLOWED}
    stale = sorted(ORM_ALLOWED - set(found))
    assert not offenders, f"seed rows with tests/support/factories.py: {offenders}"
    assert not stale, f"no longer builds rows; remove it from ORM_ALLOWED: {stale}"


def test_helpers_live_in_support_or_fakes() -> None:
    offenders = [
        _relative(path)
        for path in _test_sources()
        if _outside(path, HELPER_DIRS)
        and not (path.name.startswith("test_") or path.name == "conftest.py")
    ]
    assert not offenders, f"move shared helpers to tests/support/: {offenders}"


def test_fixture_detector_fires() -> None:
    source = "@pytest.fixture\ndef a(): ...\n@pytest.fixture(autouse=True)\nasync def b(): ...\n"
    assert _fixtures(ast.parse(source)) == {"a", "b"}


def test_no_fixture_shadows_a_root_fixture() -> None:
    root = TESTS_DIR / "conftest.py"
    shared = _fixtures(_parse(root))
    offenders = {
        _relative(path): sorted(clash)
        for path in _test_sources()
        if path != root
        if (clash := _fixtures(_parse(path)) & shared)
    }
    assert shared
    assert not offenders, f"fixture shadows one in tests/conftest.py; rename it: {offenders}"
