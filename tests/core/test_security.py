from unittest.mock import patch

from skeleton_crew.core.security import (
    check_csrf_token,
    hash_password,
    make_csrf_token,
    verify_password,
)

_SECRET = "x" * 32


def test_password_roundtrip() -> None:
    hashed = hash_password("correct horse")
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "correct horse")
    assert not verify_password(hashed, "wrong horse")


def test_garbage_hash_never_verifies() -> None:
    assert not verify_password("not-a-hash", "anything")
    assert not verify_password("", "")


def test_csrf_token_is_bound_to_session() -> None:
    token = make_csrf_token(_SECRET, "session-a")
    assert check_csrf_token(_SECRET, "session-a", token, max_age_s=60)
    assert not check_csrf_token(_SECRET, "session-b", token, max_age_s=60)


def test_csrf_token_with_other_secret_or_tampering_fails() -> None:
    token = make_csrf_token(_SECRET, "session-a")
    assert not check_csrf_token("y" * 32, "session-a", token, max_age_s=60)
    assert not check_csrf_token(_SECRET, "session-a", token[:-2] + "AA", max_age_s=60)
    assert not check_csrf_token(_SECRET, "session-a", "", max_age_s=60)


def test_expired_csrf_token_fails() -> None:
    with patch("itsdangerous.timed.time.time", return_value=1_000_000):
        token = make_csrf_token(_SECRET, "session-a")
    with patch("itsdangerous.timed.time.time", return_value=1_000_120):
        assert not check_csrf_token(_SECRET, "session-a", token, max_age_s=60)
