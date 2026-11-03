import hmac

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

_hasher = PasswordHasher()
_CSRF_SALT = "skeleton-crew.csrf"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError, InvalidHashError:
        return False


def make_csrf_token(secret: str, session_id: str) -> str:
    return URLSafeTimedSerializer(secret, salt=_CSRF_SALT).dumps(session_id)


def check_csrf_token(secret: str, session_id: str, token: str, max_age_s: int) -> bool:
    try:
        payload = URLSafeTimedSerializer(secret, salt=_CSRF_SALT).loads(token, max_age=max_age_s)
    except BadSignature:
        return False
    return isinstance(payload, str) and hmac.compare_digest(payload, session_id)
