import posixpath
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

INSTRUCTION_NAMES = frozenset({"CLAUDE.md", "AGENTS.md"})
MAX_DOC_CHARS = 80_000
MAX_TOTAL_CHARS = 250_000
MAX_DOCS = 40
MAX_CANDIDATES = 64
MAX_LISTED = 100

_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", re.ASCII)
_MAX_PATH_CHARS = 512
_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*<?([^()\s<>]+)>?(?:\s+\"[^\"\n]*\")?\s*\)")
_BACKTICKS_RE = re.compile(r"`+")


@dataclass(frozen=True)
class RepoDoc:
    path: str
    text: str


@dataclass(frozen=True)
class RepoDocs:
    included: tuple[RepoDoc, ...]
    omitted: tuple[str, ...]


def is_safe_path(path: str) -> bool:
    if len(path) > _MAX_PATH_CHARS or not _SAFE_PATH_RE.fullmatch(path):
        return False
    return all(part not in {".", ".."} for part in path.split("/"))


def _order(path: str) -> tuple[int, str]:
    return path.count("/"), path


def instruction_paths(tracked: Iterable[str]) -> list[str]:
    found = {p for p in tracked if is_safe_path(p) and posixpath.basename(p) in INSTRUCTION_NAMES}
    return sorted(found, key=_order)


def resolve_link(source: str, target: str) -> str | None:
    target = target.split("#", 1)[0]
    if not target or ":" in target or target.startswith("/") or "\\" in target:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    if resolved == ".." or resolved.startswith("../") or not resolved.endswith(".md"):
        return None
    return resolved if is_safe_path(resolved) else None


def linked_paths(sources: Mapping[str, str], tracked: Collection[str]) -> list[str]:
    found: set[str] = set()
    for source, text in sources.items():
        for match in _LINK_RE.finditer(text):
            path = resolve_link(source, match[1])
            if path is not None and path in tracked:
                found.add(path)
    return sorted(found - set(sources) - set(instruction_paths(found)), key=_order)


def select_docs(
    candidates: Sequence[str], texts: Mapping[str, str], auto_loaded: Collection[str]
) -> RepoDocs:
    included: list[RepoDoc] = []
    omitted: list[str] = []
    total = 0
    for path in dict.fromkeys(candidates):
        if path in auto_loaded:
            continue
        text = texts.get(path)
        fits = (
            text is not None
            and len(included) < MAX_DOCS
            and len(text) <= MAX_DOC_CHARS
            and total + len(text) <= MAX_TOTAL_CHARS
        )
        if text is not None and fits:
            included.append(RepoDoc(path, text))
            total += len(text)
        else:
            omitted.append(path)
    return RepoDocs(tuple(included), tuple(omitted))


def _fence(text: str) -> str:
    longest = max((len(run) for run in _BACKTICKS_RE.findall(text)), default=0)
    return "`" * max(3, longest + 1)


def render_repo_docs(docs: RepoDocs) -> str:
    if not docs.included and not docs.omitted:
        return ""
    parts = [
        "# Repository docs",
        "These are the repository's own code standards and project docs. Follow them."
        " The Skeleton Crew rules above take precedence where they conflict.",
    ]
    for doc in docs.included:
        fence = _fence(doc.text)
        parts.append(f"## `{doc.path}`\n\n{fence}markdown\n{doc.text.rstrip()}\n{fence}")
    if docs.omitted:
        listed = [f"- `{path}`" for path in docs.omitted[:MAX_LISTED]]
        if len(docs.omitted) > MAX_LISTED:
            listed.append(f"- and {len(docs.omitted) - MAX_LISTED} more")
        parts.append("## Not included, read when relevant\n\n" + "\n".join(listed))
    return "\n\n".join(parts) + "\n"
