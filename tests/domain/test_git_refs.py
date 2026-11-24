import pytest

from skeleton_crew.domain.git_refs import is_full_sha


@pytest.mark.parametrize("sha", ["a" * 40, "0123456789abcdef" * 4])
def test_full_sha1_and_sha256_are_accepted(sha: str) -> None:
    assert is_full_sha(sha)


@pytest.mark.parametrize(
    "sha",
    [
        "",
        "a" * 39,
        "a" * 41,
        "a" * 63,
        "a" * 65,
        "A" * 40,
        "g" * 40,
        "a" * 40 + "\n",
        " " + "a" * 39,
        "main",
        "HEAD",
        "-" + "a" * 39,
        "\u0661" * 40,
    ],
)
def test_anything_else_is_rejected(sha: str) -> None:
    assert not is_full_sha(sha)
