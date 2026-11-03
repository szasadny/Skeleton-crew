from skeleton_crew.domain.comment_policy import AddedLine
from skeleton_crew.domain.merge_policy import check_resolution, conflict_markers


def _lines(*texts: str) -> list[AddedLine]:
    return [AddedLine("app.py", i, text) for i, text in enumerate(texts, start=1)]


def test_left_over_markers_are_reported() -> None:
    found = conflict_markers(_lines("<<<<<<< ours", "x = 1", "=======", "x = 2", ">>>>>>> main"))
    assert found == [
        "app.py:1: unresolved conflict marker",
        "app.py:5: unresolved conflict marker",
    ]


def test_bare_markers_without_a_label_are_reported() -> None:
    assert len(conflict_markers(_lines("<<<<<<<", ">>>>>>>"))) == 2


def test_marker_text_inside_code_is_not_a_marker() -> None:
    assert conflict_markers(_lines('MARK = "<<<<<<< "', "  <<<<<<< indented", "<<<<<<<<< x")) == []


def test_resolution_may_only_touch_conflicted_files() -> None:
    assert check_resolution(["app.py"], {"app.py", "README.md"}) == []
    assert check_resolution(["app.py", ".github/workflows/ci.yml"], {"app.py"}) == [
        ".github/workflows/ci.yml: changed outside the merge conflict"
    ]


def test_new_or_renamed_file_outside_the_conflict_is_rejected() -> None:
    assert check_resolution(["app.py", "app_renamed.py"], {"app.py"}) == [
        "app_renamed.py: changed outside the merge conflict"
    ]


def test_path_matching_is_exact() -> None:
    assert check_resolution(["App.py", "app.py/../x"], {"app.py"}) == [
        "App.py: changed outside the merge conflict",
        "app.py/../x: changed outside the merge conflict",
    ]
