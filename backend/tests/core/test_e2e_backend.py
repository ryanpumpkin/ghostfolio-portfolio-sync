"""Tests for backend-side E2E wrapped credential unwrapping."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.e2e_backend import (
    WrappedCredentialError,
    WrappedCredentialExpiredError,
    unwrap_from_backend,
)


def test_unwrap_from_dart_generated_fixture_roundtrip() -> None:
    fixture = {
        "keyB64": "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=",
        "token": "eyJ2IjoxLCJleHBpcmVzQXQiOjE3NjczMjMxNjUwMDAsImN0IjoiZXlKdUlqb2lhSGREZHpBek4wOHpOMEZOV1hOSVVTSXNJbU1pT2lJM2RHNTRNa1ZaV1cxc1MxZElVSGxIZGtwNGVUaEtlVXR5T1ZvM01WcFFSRVZRUnk4aUxDSnRJam9pVGpoV1JXbHZMM2cxV1dWVksyVktVRWh1VFdoTFFUMDlJbjA9In0=",
        "plaintext": '{"apiKey":"k","secret":"s"}',
    }
    key = base64.b64decode(fixture["keyB64"].encode("ascii"))
    now = datetime(2026, 1, 2, 3, 5, 0, tzinfo=UTC)

    plaintext = unwrap_from_backend(fixture["token"], key=key, now=now)

    assert plaintext == fixture["plaintext"]


def test_unwrap_accepts_ct_object_shape() -> None:
    key = bytes(range(1, 33))
    nonce = bytes(range(12))
    plaintext = b'{"appKey":"k","appSecret":"s","accessToken":"t"}'
    encrypted = AESGCM(key).encrypt(nonce, plaintext, None)
    cipher = encrypted[:-16]
    mac = encrypted[-16:]
    payload = {
        "v": 1,
        "expiresAt": int((datetime.now(UTC) + timedelta(minutes=2)).timestamp() * 1000),
        "ct": {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "cipherBytes": base64.b64encode(cipher).decode("ascii"),
            "mac": base64.b64encode(mac).decode("ascii"),
        },
    }
    token = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode(
        "ascii"
    )

    out = unwrap_from_backend(token, key=key)

    assert out == plaintext.decode("utf-8")


def test_unwrap_rejects_expired_token() -> None:
    key = bytes(range(1, 33))
    payload = {"v": 1, "expiresAt": 1, "ct": "e30="}
    token = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    with pytest.raises(WrappedCredentialExpiredError):
        unwrap_from_backend(token, key=key, now=datetime.now(UTC))


def test_unwrap_rejects_malformed_token() -> None:
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend("not-base64!!", key=bytes(range(1, 33)))


# ---------------------------------------------------------------------------
# Error-path coverage
# ---------------------------------------------------------------------------


def _encode(payload: object) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    ).decode("ascii")


def test_unwrap_rejects_empty_token() -> None:
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend("", key=bytes(range(1, 33)))


def test_unwrap_rejects_wrong_key_length() -> None:
    payload = {"v": 1, "expiresAt": 1, "ct": "e30="}
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(_encode(payload), key=b"short-key")


def test_unwrap_rejects_payload_that_is_not_an_object() -> None:
    token = base64.b64encode(b'"just-a-string"').decode("ascii")
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_invalid_json_body() -> None:
    token = base64.b64encode(b"{ not valid json").decode("ascii")
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_missing_expires_at() -> None:
    token = _encode({"v": 1, "ct": "e30="})
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_ciphertext_field_of_wrong_type() -> None:
    token = _encode(
        {
            "v": 1,
            "expiresAt": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
            "ct": 42,  # neither str nor dict
        },
    )
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_ciphertext_str_with_bad_json() -> None:
    bad_ct = base64.b64encode(b"not-json").decode("ascii")
    token = _encode(
        {
            "v": 1,
            "expiresAt": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
            "ct": bad_ct,
        },
    )
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_ciphertext_str_decoded_to_non_object() -> None:
    inner = base64.b64encode(b'"hello"').decode("ascii")
    token = _encode(
        {
            "v": 1,
            "expiresAt": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
            "ct": inner,
        },
    )
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_b64_field_of_wrong_type() -> None:
    token = _encode(
        {
            "v": 1,
            "expiresAt": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
            "ct": {"nonce": 123, "cipherBytes": "AA==", "mac": "AA=="},
        },
    )
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=bytes(range(1, 33)))


def test_unwrap_rejects_decrypt_failure() -> None:
    # Valid envelope shape, but wrong key → AESGCM raises.
    real_key = bytes(range(1, 33))
    wrong_key = bytes(range(33, 65))
    nonce = bytes(range(12))
    cipher = AESGCM(real_key).encrypt(nonce, b"secret", None)
    token = _encode(
        {
            "v": 1,
            "expiresAt": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
            "ct": {
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "cipherBytes": base64.b64encode(cipher[:-16]).decode("ascii"),
                "mac": base64.b64encode(cipher[-16:]).decode("ascii"),
            },
        },
    )
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=wrong_key)


def test_unwrap_rejects_non_utf8_plaintext() -> None:
    # AES-GCM succeeds but bytes aren't valid UTF-8.
    key = bytes(range(1, 33))
    nonce = bytes(range(12))
    plaintext = b"\xff\xfe\xfd"  # invalid UTF-8
    cipher = AESGCM(key).encrypt(nonce, plaintext, None)
    token = _encode(
        {
            "v": 1,
            "expiresAt": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
            "ct": {
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "cipherBytes": base64.b64encode(cipher[:-16]).decode("ascii"),
                "mac": base64.b64encode(cipher[-16:]).decode("ascii"),
            },
        },
    )
    with pytest.raises(WrappedCredentialError):
        unwrap_from_backend(token, key=key)
