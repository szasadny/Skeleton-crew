from pathlib import Path

import pytest

from skeleton_crew.runtime.task_dirs import list_task_keys, remove_task_dirs, task_paths


@pytest.mark.parametrize("key", ["../x", "a/b", "", ".hidden", "x" * 65, "w-1", "worker-1/.."])
def test_task_paths_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="task key"):
        task_paths(tmp_path, key)


def test_task_paths_layout(tmp_path: Path) -> None:
    paths = task_paths(tmp_path, "worker-7")
    assert paths.root == tmp_path / "worker-7"
    assert paths.controller_repo.parent == paths.agent_repo.parent == paths.root


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    return outside


def test_remove_task_dirs_removes_only_listed_task_keys(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    for name in ("worker-1", "worker-2", "notes"):
        (tasks / name / "transcripts").mkdir(parents=True)

    removed = remove_task_dirs(tasks, ["worker-1", "worker-3", "notes", "..", ""])

    assert removed == ["worker-1"]
    assert sorted(p.name for p in tasks.iterdir()) == ["notes", "worker-2"]
    assert tmp_path.exists()


def test_remove_task_dirs_refuses_a_symlinked_task_dir(tmp_path: Path) -> None:
    outside = _outside(tmp_path)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "worker-1").symlink_to(outside, target_is_directory=True)

    assert remove_task_dirs(tasks, ["worker-1"]) == []
    assert (outside / "keep.txt").read_text() == "keep"


def test_remove_task_dirs_does_not_follow_links_inside(tmp_path: Path) -> None:
    outside = _outside(tmp_path)
    task = tmp_path / "tasks" / "refine-4"
    task.mkdir(parents=True)
    (task / "escape").symlink_to(outside, target_is_directory=True)

    assert remove_task_dirs(tmp_path / "tasks", ["refine-4"]) == ["refine-4"]
    assert not task.exists()
    assert (outside / "keep.txt").read_text() == "keep"


def test_remove_task_dirs_ignores_a_plain_file(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "run-3-selector").write_text("file")

    assert remove_task_dirs(tasks, ["run-3-selector"]) == []
    assert (tasks / "run-3-selector").is_file()


def test_list_task_keys_skips_links_files_and_other_names(tmp_path: Path) -> None:
    outside = _outside(tmp_path)
    tasks = tmp_path / "tasks"
    for name in ("worker-1", "refine-2", "run-3-selector", "notes", "worker-x"):
        (tasks / name).mkdir(parents=True)
    (tasks / "worker-4").symlink_to(outside, target_is_directory=True)
    (tasks / "worker-5").write_text("file")

    assert list_task_keys(tasks) == ["refine-2", "run-3-selector", "worker-1"]
    assert list_task_keys(tmp_path / "missing") == []
