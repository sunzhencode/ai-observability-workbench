"""Authenticated local-secret storage and redaction regressions."""

from __future__ import annotations

import pytest

from app.crypto import (
    SecretBox,
    SecretIntegrityError,
    SecretUnavailableError,
    apply_secret_update,
    redact_sensitive,
)


def test_fernet_round_trip_and_key_mismatch_fail_closed() -> None:
    box = SecretBox("test-master-key-one")
    envelope = box.encrypt("super-secret-value")

    assert box.decrypt(envelope) == "super-secret-value"
    assert envelope["algorithm"] == "fernet"
    assert envelope["key_id"] == box.key_id
    assert "super-secret-value" not in repr(envelope)

    with pytest.raises(SecretIntegrityError):
        SecretBox("different-master-key").decrypt(envelope)


def test_missing_key_and_secret_three_state_are_unambiguous() -> None:
    with pytest.raises(SecretUnavailableError):
        SecretBox("")

    box = SecretBox("test-master-key")
    original = box.encrypt("old")
    assert apply_secret_update(original, "KEEP", None, box) == original
    replacement = apply_secret_update(original, "REPLACE", "new", box)
    assert replacement is not None
    assert box.decrypt(replacement) == "new"
    assert apply_secret_update(original, "CLEAR", None, box) is None
    with pytest.raises(ValueError, match="REPLACE"):
        apply_secret_update(None, "REPLACE", "", box)
    with pytest.raises(ValueError, match="required"):
        apply_secret_update(original, "CLEAR", None, box, required=True)


def test_redactor_never_returns_secret_or_ciphertext() -> None:
    redacted = redact_sensitive(
        {
            "token": "plain-token",
            "password": "plain-password",
            "nested": {"webhook_envelope": {"ciphertext": "cipher-value"}},
            "safe": "visible",
        }
    )

    rendered = repr(redacted)
    assert "plain-token" not in rendered
    assert "plain-password" not in rendered
    assert "cipher-value" not in rendered
    assert redacted["safe"] == "visible"
