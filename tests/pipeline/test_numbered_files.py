from pathlib import Path

from skeleton_crew.domain.settings import Role
from skeleton_crew.pipeline.sessions import next_numbered, next_transcript


def test_first_file_gets_number_one(tmp_path: Path) -> None:
    assert next_numbered(tmp_path / "t", "check", ".log") == tmp_path / "t" / "check-1.log"


def test_a_gap_never_reuses_an_existing_number(tmp_path: Path) -> None:
    for name in ("check-1.log", "check-3.log"):
        (tmp_path / name).write_text("")
    assert next_numbered(tmp_path, "check", ".log") == tmp_path / "check-4.log"


def test_other_names_do_not_count(tmp_path: Path) -> None:
    for name in ("setup-7.log", "check-x.log", "check-2.log.bak", "precheck-9.log"):
        (tmp_path / name).write_text("")
    assert next_numbered(tmp_path, "check", ".log") == tmp_path / "check-1.log"


def test_transcripts_continue_after_a_missing_one(tmp_path: Path) -> None:
    for name in ("worker-2.jsonl", "reviewer-5.jsonl"):
        (tmp_path / name).write_text("")
    assert next_transcript(tmp_path, Role.WORKER) == tmp_path / "worker-3.jsonl"
