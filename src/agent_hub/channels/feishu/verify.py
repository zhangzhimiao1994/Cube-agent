from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class FeishuVerificationError(RuntimeError):
    """A Feishu event failed authentication or integrity checks."""


@dataclass(frozen=True, slots=True)
class VerifiedFeishuPayload:
    payload: dict[str, Any]
    challenge: str | None = None


class FeishuVerifier:
    """Verify and decode Feishu callback payloads.

    Feishu signs webhook bodies with SHA-256 over timestamp, nonce, encrypt key,
    and raw body bytes. Encrypted callbacks carry an ``encrypt`` field containing
    AES-CBC ciphertext; the AES key is SHA-256(encrypt_key).
    """

    def __init__(
        self,
        *,
        app_id: str,
        verification_token: str,
        encrypt_key: str,
        allowed_tenant_keys: frozenset[str] = frozenset(),
        timestamp_tolerance_seconds: int = 300,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._app_id = app_id
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._allowed_tenant_keys = allowed_tenant_keys
        self._timestamp_tolerance_seconds = timestamp_tolerance_seconds
        self._clock = clock or time.time

    def verify_webhook(
        self,
        body: bytes,
        *,
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
    ) -> VerifiedFeishuPayload:
        if timestamp or nonce or signature:
            self._verify_signature(body, timestamp=timestamp, nonce=nonce, signature=signature)
        payload = _loads_json_object(body)
        payload = self._decrypt_if_needed(payload)
        return self.verify_payload(payload)

    def verify_payload(self, payload: dict[str, Any]) -> VerifiedFeishuPayload:
        self._verify_token(payload)
        self._verify_app_identity(payload)
        challenge = payload.get("challenge")
        if challenge is not None and not isinstance(challenge, str):
            raise FeishuVerificationError("invalid challenge")
        return VerifiedFeishuPayload(payload=payload, challenge=challenge)

    def sign(self, body: bytes, *, timestamp: str, nonce: str) -> str:
        return _signature(
            body,
            timestamp=timestamp,
            nonce=nonce,
            encrypt_key=self._encrypt_key,
        )

    def encrypt_for_test(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        padded = _pkcs7_pad(raw)
        key = hashlib.sha256(self._encrypt_key.encode()).digest()
        iv = hashlib.md5(self._encrypt_key.encode()).digest()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(iv + ciphertext).decode()

    def _verify_signature(
        self,
        body: bytes,
        *,
        timestamp: str | None,
        nonce: str | None,
        signature: str | None,
    ) -> None:
        if not timestamp or not nonce or not signature:
            raise FeishuVerificationError("missing signature headers")
        try:
            timestamp_seconds = int(timestamp)
        except ValueError as error:
            raise FeishuVerificationError("invalid timestamp") from error
        if abs(self._clock() - timestamp_seconds) > self._timestamp_tolerance_seconds:
            raise FeishuVerificationError("stale timestamp")
        expected = self.sign(body, timestamp=timestamp, nonce=nonce)
        if not hmac.compare_digest(expected, signature):
            raise FeishuVerificationError("invalid signature")

    def _verify_token(self, payload: dict[str, Any]) -> None:
        token = payload.get("token")
        header = payload.get("header")
        if token is None and isinstance(header, dict):
            token = header.get("token")
        if token != self._verification_token:
            raise FeishuVerificationError("invalid verification token")

    def _verify_app_identity(self, payload: dict[str, Any]) -> None:
        header = payload.get("header")
        if header is None and payload.get("challenge") is not None:
            return
        if not isinstance(header, dict):
            raise FeishuVerificationError("missing event header")
        app_id = header.get("app_id")
        if app_id != self._app_id:
            raise FeishuVerificationError("invalid app identity")
        tenant_key = header.get("tenant_key")
        if not isinstance(tenant_key, str) or not tenant_key:
            raise FeishuVerificationError("missing tenant identity")
        if self._allowed_tenant_keys and tenant_key not in self._allowed_tenant_keys:
            raise FeishuVerificationError("invalid tenant identity")

    def _decrypt_if_needed(self, payload: dict[str, Any]) -> dict[str, Any]:
        encrypted = payload.get("encrypt")
        if encrypted is None:
            return payload
        if not isinstance(encrypted, str):
            raise FeishuVerificationError("invalid encrypted event")
        try:
            decoded = base64.b64decode(encrypted)
            decrypted = _decrypt_aes_cbc(decoded, self._encrypt_key)
        except Exception as error:
            raise FeishuVerificationError("invalid encrypted event") from error
        return _loads_json_object(decrypted)


def _signature(
    body: bytes,
    *,
    timestamp: str,
    nonce: str,
    encrypt_key: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(timestamp.encode())
    digest.update(nonce.encode())
    digest.update(encrypt_key.encode())
    digest.update(body)
    return digest.hexdigest()


def _decrypt_aes_cbc(ciphertext_with_iv: bytes, encrypt_key: str) -> bytes:
    if len(ciphertext_with_iv) < 32 or len(ciphertext_with_iv) % 16 != 0:
        raise ValueError("invalid ciphertext length")
    iv = ciphertext_with_iv[:16]
    ciphertext = ciphertext_with_iv[16:]
    key = hashlib.sha256(encrypt_key.encode()).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return _pkcs7_unpad(padded)


def _pkcs7_pad(value: bytes) -> bytes:
    padding_length = 16 - (len(value) % 16)
    return value + bytes([padding_length]) * padding_length


def _pkcs7_unpad(value: bytes) -> bytes:
    if not value:
        raise ValueError("empty plaintext")
    padding_length = value[-1]
    if padding_length < 1 or padding_length > 16:
        raise ValueError("invalid padding")
    if value[-padding_length:] != bytes([padding_length]) * padding_length:
        raise ValueError("invalid padding")
    return value[:-padding_length]


def _loads_json_object(value: bytes) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise FeishuVerificationError("payload must be an object")
    return parsed


__all__ = [
    "FeishuVerificationError",
    "FeishuVerifier",
    "VerifiedFeishuPayload",
]
