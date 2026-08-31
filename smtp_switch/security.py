"""Password hashing for sending-service accounts and dashboard users."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
