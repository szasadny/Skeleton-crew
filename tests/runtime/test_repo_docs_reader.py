from pathlib import Path

from runtime_helpers import Repo

from skeleton_crew.domain.repo_docs import MAX_DOC_CHARS
from skeleton_crew.runtime.repo_docs_reader import read_repo_docs


def _docs_repo(root: Path) -> Repo:
    repo = Repo(root)
    repo.write("CLAUDE.md", "root claude\n")
    repo.write(".claude/CLAUDE.md", "See [arch](./ARCHITECTURE.md).\n")
    repo.write(".claude/ARCHITECTURE.md", "Layers. See [deep](../deep.md).\n")
    repo.write("deep.md", "too deep\n")
    repo.write("AGENTS.md", "Read [security](SECURITY.md) and [gone](missing.md).\n")
    repo.write("SECURITY.md", "threats\n")
    repo.write("pkg/AGENTS.md", "nested ü\n")
    repo.write("README.md", "readme\n")
    return repo


def test_read_repo_docs_collects_instruction_and_linked_docs(tmp_path: Path) -> None:
    repo = _docs_repo(tmp_path / "repo")
    repo.commit()

    sources = read_repo_docs(repo.root)

    assert sources.candidates == (
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/CLAUDE.md",
        "pkg/AGENTS.md",
        "SECURITY.md",
        ".claude/ARCHITECTURE.md",
    )
    assert sources.texts["pkg/AGENTS.md"] == "nested ü\n"
    assert "deep.md" not in sources.texts
    assert "README.md" not in sources.texts


def test_read_repo_docs_skips_symlinks_and_untracked_files(tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("host secret\n")
    repo = Repo(tmp_path / "repo")
    repo.write("AGENTS.md", "[s](linked.md) [u](untracked.md)\n")
    (repo.root / "linked.md").symlink_to(secret)
    (repo.root / "sub").mkdir()
    (repo.root / "sub" / "AGENTS.md").symlink_to(secret)
    repo.commit()
    repo.write("untracked.md", "not committed\n")

    sources = read_repo_docs(repo.root)

    assert sources.candidates == ("AGENTS.md",)
    assert set(sources.texts) == {"AGENTS.md"}


def test_read_repo_docs_replaces_invalid_utf8_and_bounds_reads(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "repo")
    repo.write("AGENTS.md", b"ok \xff\n")
    repo.write("big/AGENTS.md", "x" * (MAX_DOC_CHARS * 5))
    repo.commit()

    texts = read_repo_docs(repo.root).texts

    assert texts["AGENTS.md"] == "ok �\n"
    assert MAX_DOC_CHARS < len(texts["big/AGENTS.md"]) < MAX_DOC_CHARS * 5
