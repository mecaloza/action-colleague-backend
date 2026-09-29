from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core import security
from app.core.config import get_settings


def test_hash_and_verify_roundtrip():
    hashed = security.hash_password("s3cret-pass")
    assert hashed.startswith("$2b$")
    assert security.verify_password("s3cret-pass", hashed)
    assert not security.verify_password("wrong", hashed)


# Generated with the previous stack (passlib 1.7.4 + bcrypt 4.0.1): existing users must keep logging in.
PASSLIB_HASHES = [
    ("legacy-pass-2024", "$2b$12$jZfkKNA/Rn8poKME0TL4ouR75woBuOhjbW6ynsVswuhxvRaGtHCv."),
    ("x" * 100, "$2b$12$4etwHnKc7ZRLE528XJzyZuqDbGwR2Pv/ZGG7uEUf7o7sD7Q3ocm6."),
    ("a" + "ñ" * 40, "$2b$12$FImNOHVya42B6G/lZv2ouO4QS7kBVfPgs9CCP9VbGlwsJjAOkNgTi"),
]


@pytest.mark.parametrize(("password", "legacy_hash"), PASSLIB_HASHES)
def test_verifies_hashes_created_by_the_previous_passlib_setup(password, legacy_hash):
    assert security.verify_password(password, legacy_hash)


def test_passwords_longer_than_72_bytes_are_truncated_consistently():
    long_password = "x" * 100
    hashed = security.hash_password(long_password)
    assert security.verify_password(long_password, hashed)
    assert security.verify_password("x" * 72, hashed)


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash"])
def test_verify_rejects_empty_or_malformed_hash(bad_hash):
    assert not security.verify_password("anything", bad_hash)


def test_access_token_roundtrip():
    token = security.create_access_token(7, "admin")
    claims = security.decode_access_token(token)
    assert claims["sub"] == "7"
    assert claims["role"] == "admin"


def test_expired_access_token_is_rejected():
    expired = jwt.encode(
        {"sub": "7", "type": "access", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_access_token(expired)


def test_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}, "x" * 40, algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        security.decode_access_token(forged)


def test_non_access_token_type_is_rejected():
    token = jwt.encode(
        {"sub": "1", "type": "refresh", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        security.decode_access_token(token)


def test_refresh_tokens_are_random_and_hashed():
    first, second = security.new_refresh_token(), security.new_refresh_token()
    assert first != second
    assert security.hash_refresh_token(first) == security.hash_refresh_token(first)
    assert len(security.hash_refresh_token(first)) == 64


def test_tokens_issued_before_the_type_claim_are_still_accepted():
    # python-jose tokens from the previous version had no "type" claim.
    legacy = jwt.encode(
        {"sub": "3", "role": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert security.decode_access_token(legacy)["sub"] == "3"
