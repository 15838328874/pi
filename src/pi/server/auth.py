"""Authentication: PBKDF2 password hashing (stdlib) + JWT (HS256, PyJWT)."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid

import jwt

_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        ).hex()
        return secrets.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def create_token(username: str, secret: str, ttl_minutes: int) -> str:
    # iat keeps sub-second resolution: the revocation epoch is written with the
    # same clock, and int-truncating both would make a token minted right after
    # a deregistration/re-registration in the same second look pre-epoch.
    now = time.time()
    payload = {
        "sub": username,
        "iat": now,
        "exp": int(now) + ttl_minutes * 60,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> dict:
    """Raises jwt.InvalidTokenError (or subclass) on invalid/expired tokens."""
    return jwt.decode(token, secret, algorithms=["HS256"])
