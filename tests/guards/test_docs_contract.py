import ast
import json
import re
from pathlib import Path

import skeleton_crew
from skeleton_crew.agents.registry import available_backends
from skeleton_crew.classifiers.registry import available_classifiers
from skeleton_crew.core.config import credential_setting
from skeleton_crew.domain.branch_policy import BRANCH_PREFIX
from skeleton_crew.domain.repo_policy import REQUIRED_PERMISSIONS

SRC_DIR = Path(skeleton_crew.__file__).parent
REPO_DIR = SRC_DIR.parents[1]
ARCHITECTURE = REPO_DIR / "ARCHITECTURE.md"
AGENTS = REPO_DIR / "AGENTS.md"
SECURITY = REPO_DIR / "SECURITY.md"
ENV_EXAMPLE = REPO_DIR / ".env.example"
APP_MANIFEST = REPO_DIR / "docs" / "github-app-manifest.json"
GUARDS_DIR = REPO_DIR / "tests" / "guards"
PATH_CHECKED_DOCS = (ARCHITECTURE, AGENTS, SECURITY)
MAX_PROSE_LINE_CHARS = 240
# Only ever lower. A doc grows with the number of modules, never with prose: trim before raising.
ARCHITECTURE_TEXT_BYTES = 13_000
ARCHITECTURE_BYTES_PER_MODULE = 100
AGENTS_MAX_BYTES = 11_000
_UNLISTED_DIRS = ("migrations/versions",)
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_PLACEHOLDER = re.compile(r"<[^>]+>")
_PLACEHOLDER_CHARS = frozenset("<>*{}…")
_PATH_SUFFIXES = (".py", ".md", ".yml", ".json", ".toml", ".sh", ".j2", "/")
_HISTORY = re.compile(
    r"\b(previously|formerly|used to|was renamed|no longer|legacy|deprecated)\b", re.IGNORECASE
)
_STRUCTURE_LINE = re.compile(r"^  ([a-z_]+)/? ", re.MULTILINE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tokens(doc: Path) -> list[str]:
    return _BACKTICKED.findall(_text(doc))


def _modules() -> list[str]:
    return sorted(
        path.relative_to(SRC_DIR).as_posix()
        for path in SRC_DIR.rglob("*.py")
        if path.name != "__init__.py"
        and not path.relative_to(SRC_DIR).as_posix().startswith(_UNLISTED_DIRS)
    )


def _module_patterns() -> list[re.Pattern[str]]:
    patterns = []
    for token in _tokens(ARCHITECTURE):
        if token.endswith(".py"):
            parts = (re.escape(part) for part in _PLACEHOLDER.split(token))
            patterns.append(re.compile("[a-z0-9_]+".join(parts)))
    return patterns


def test_every_module_has_a_home_in_the_architecture_doc() -> None:
    patterns = _module_patterns()
    missing = [
        module
        for module in _modules()
        if not any(pattern.fullmatch(module) for pattern in patterns)
    ]
    assert not missing, f"add one row to ARCHITECTURE.md §0 for: {missing}"


def _is_path(token: str) -> bool:
    return (
        "/" in token
        and " " not in token
        and token.endswith(_PATH_SUFFIXES)
        and _PLACEHOLDER_CHARS.isdisjoint(token)
        and token != BRANCH_PREFIX
    )


def _exists(token: str) -> bool:
    return any((base / token).exists() for base in (REPO_DIR, SRC_DIR))


def test_doc_paths_exist() -> None:
    missing = sorted(
        f"{doc.name}: {token}"
        for doc in PATH_CHECKED_DOCS
        for token in _tokens(doc)
        if _is_path(token) and not _exists(token)
    )
    assert not missing, f"path in a doc does not exist; fix or remove it: {missing}"


def _long_prose_lines(doc: Path) -> list[int]:
    found, fenced = [], False
    for number, line in enumerate(_text(doc).splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and not line.startswith("|") and len(line) > MAX_PROSE_LINE_CHARS:
            found.append(number)
    return found


def _size_limits() -> dict[Path, int]:
    architecture = ARCHITECTURE_TEXT_BYTES + ARCHITECTURE_BYTES_PER_MODULE * len(_modules())
    return {ARCHITECTURE: architecture, AGENTS: AGENTS_MAX_BYTES}


def test_docs_stay_short() -> None:
    offenders = [
        f"{doc.name}: {doc.stat().st_size} bytes (max {limit}), long lines {_long_prose_lines(doc)}"
        for doc, limit in _size_limits().items()
        if doc.stat().st_size > limit or _long_prose_lines(doc)
    ]
    assert not offenders, f"one short sentence per fact; behaviour belongs in a test: {offenders}"


def test_docs_describe_the_current_state() -> None:
    offenders = {
        doc.name: hits for doc in (ARCHITECTURE, AGENTS) if (hits := _HISTORY.findall(_text(doc)))
    }
    assert not offenders, f"history in a doc; describe only what is true now: {offenders}"


def _section(doc: Path, heading: str) -> str:
    text = _text(doc)
    start = text.index(f"\n{heading}")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


def _model_classes() -> set[str]:
    return {
        node.name
        for path in (SRC_DIR / "models").glob("*.py")
        if path.name != "types.py"
        for node in ast.parse(_text(path)).body
        if isinstance(node, ast.ClassDef)
    }


def test_data_model_lists_every_entity() -> None:
    rows = re.findall(r"^\| `(\w+)` \|", _section(ARCHITECTURE, "## 5. "), re.MULTILINE)
    assert set(rows) == _model_classes()


def test_backend_and_classifier_lists_match_the_registries() -> None:
    cross_cutting = _section(ARCHITECTURE, "## 6. ")
    backends = _section_line(cross_cutting, "- Backends:")
    providers = _section_line(cross_cutting, "- Providers:")
    assert set(_BACKTICKED.findall(backends)) - {"agents/backends/<name>.py"} == {
        spec.name for spec in available_backends()
    }
    assert set(_BACKTICKED.findall(providers)) - {"classifiers/<name>.py"} == {
        spec.name for spec in available_classifiers()
    }


def _section_line(section: str, prefix: str) -> str:
    return next(line for line in section.splitlines() if line.startswith(prefix))


def test_every_guard_is_listed_in_the_contract() -> None:
    contract = _section(ARCHITECTURE, "## 7. ")
    missing = sorted(
        path.name for path in GUARDS_DIR.glob("test_*.py") if path.name not in contract
    )
    assert not missing, f"name the guard in ARCHITECTURE.md §7: {missing}"


def test_structure_block_lists_every_package() -> None:
    structure = _text(AGENTS).split("```text\nsrc/skeleton_crew/\n", 1)[1].split("tests/", 1)[0]
    listed = set(_STRUCTURE_LINE.findall(structure))
    packages = {path.parent.name for path in SRC_DIR.glob("*/__init__.py")}
    assert listed == packages


def test_env_example_lists_every_declared_credential() -> None:
    declared = {name for spec in available_backends() for name in spec.credentials} | {
        spec.credential for spec in available_classifiers()
    }
    example = _text(ENV_EXAMPLE)
    missing = sorted(
        credential_setting(name) for name in declared if credential_setting(name) not in example
    )
    assert not missing, f"document the credential in .env.example: {missing}"


def test_app_manifest_asks_for_exactly_the_required_permissions() -> None:
    manifest = json.loads(_text(APP_MANIFEST))
    assert manifest["default_permissions"] == dict(REQUIRED_PERMISSIONS)
