"""Authenticated encryption and centralized redaction for local Web secrets."""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from typing import Any, Callable, Literal

from cryptography.fernet import Fernet, InvalidToken

from app.platform.redaction import SENSITIVE_KEY_PARTS, redact_sensitive


class SecretError(RuntimeError):
    """Base class for errors that must fail closed."""


class SecretUnavailableError(SecretError):
    pass


class SecretIntegrityError(SecretError):
    pass


SecretAction = Literal["KEEP", "REPLACE", "CLEAR"]
class SecretBox:
    """Fernet envelope derived from one bootstrap-only master key."""

    def __init__(self, master_key: str) -> None:
        value = str(master_key or "")
        if not value:
            raise SecretUnavailableError("master key is not configured")
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        self.key_id = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, plaintext: str) -> dict[str, Any]:
        value = str(plaintext)
        if not value:
            raise ValueError("secret must not be empty")
        return {
            "version": 1,
            "algorithm": "fernet",
            "key_id": self.key_id,
            "ciphertext": self._fernet.encrypt(value.encode("utf-8")).decode("ascii"),
        }

    def decrypt(self, envelope: dict[str, Any] | None) -> str:
        if not isinstance(envelope, dict):
            raise SecretIntegrityError("secret envelope is missing")
        if (
            envelope.get("version") != 1
            or envelope.get("algorithm") != "fernet"
            or envelope.get("key_id") != self.key_id
        ):
            raise SecretIntegrityError("secret envelope does not match active key")
        ciphertext = envelope.get("ciphertext")
        if not isinstance(ciphertext, str) or not ciphertext:
            raise SecretIntegrityError("secret envelope is invalid")
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretIntegrityError("secret authentication failed") from exc


class LazySecretBox:
    """A :class:`SecretBox` constructed on first actual use.

    Building it eagerly meant a data source with no credentials at all could not
    be saved without a master key: the caller resolved the box before knowing
    whether any secret needed encrypting, and a missing key surfaced as a bare
    503 with no hint about what to do. The key is only genuinely required to
    store or read a secret, so the construction is deferred to that moment.
    """

    def __init__(self, key_provider: Callable[[], str]) -> None:
        self._key_provider = key_provider
        self._box: SecretBox | None = None

    def _resolve(self) -> SecretBox:
        if self._box is None:
            self._box = SecretBox(self._key_provider())
        return self._box

    @property
    def key_id(self) -> str:
        return self._resolve().key_id

    def encrypt(self, plaintext: str) -> dict[str, Any]:
        return self._resolve().encrypt(plaintext)

    def decrypt(self, envelope: dict[str, Any] | None) -> str:
        return self._resolve().decrypt(envelope)


def apply_secret_update(
    existing: dict[str, Any] | None,
    action: SecretAction | str,
    value: str | None,
    box: SecretBox,
    *,
    required: bool = False,
) -> dict[str, Any] | None:
    normalized = str(action).upper()
    if normalized == "KEEP":
        if required and existing is None:
            raise ValueError("secret is required")
        return deepcopy(existing)
    if normalized == "CLEAR":
        if required:
            raise ValueError("secret is required and cannot be cleared")
        return None
    if normalized == "REPLACE":
        if value is None or not str(value):
            raise ValueError("REPLACE requires a non-empty secret value")
        return box.encrypt(str(value))
    raise ValueError("secret action must be KEEP, REPLACE, or CLEAR")


def secret_public_state(envelope: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "configured": envelope is not None,
        "hint": "configured" if envelope is not None else None,
    }
