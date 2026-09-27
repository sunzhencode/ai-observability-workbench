"""Opaque HMAC-signed keyset cursor bound to normalized filters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from typing import Any


class CursorError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CursorPosition:
    position: int


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class SignedCursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("cursor signing secret must be at least 32 bytes")
        self._secret = bytes(secret)

    def encode(self, *, position: int, filters: dict[str, Any]) -> str:
        if position < 0:
            raise ValueError("cursor position cannot be negative")
        body = _canonical(
            {
                "v": 1,
                "p": position,
                "f": hashlib.sha256(_canonical(filters)).hexdigest(),
            }
        )
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{_b64encode(body)}.{_b64encode(signature)}"

    def decode(self, token: str, *, filters: dict[str, Any]) -> CursorPosition:
        try:
            encoded_body, encoded_signature = token.split(".", 1)
            body = _b64decode(encoded_body)
            supplied = _b64decode(encoded_signature)
            expected = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                raise CursorError("CURSOR_INVALID")
            payload = json.loads(body)
            position = payload["p"]
            fingerprint = payload["f"]
            if payload["v"] != 1 or type(position) is not int or position < 0:
                raise CursorError("CURSOR_INVALID")
        except CursorError:
            raise
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CursorError("CURSOR_INVALID") from exc
        expected_filter = hashlib.sha256(_canonical(filters)).hexdigest()
        if not hmac.compare_digest(str(fingerprint), expected_filter):
            raise CursorError("CURSOR_FILTER_MISMATCH")
        return CursorPosition(position=position)
