"""Reusable API v1 envelope, idempotency and version contracts."""

from __future__ import annotations

import pytest

from app.api.v1.contracts import (
    format_etag,
    parse_expected_version,
    validate_idempotency_key,
)
from app.platform.errors import SafeApiError


def test_expected_version_accepts_body_or_etag_and_rejects_disagreement() -> None:
    assert parse_expected_version(if_match=None, body_version=7) == 7
    assert parse_expected_version(if_match='W/"7"', body_version=None) == 7
    assert parse_expected_version(if_match='"7"', body_version=7) == 7
    assert format_etag(7) == 'W/"7"'

    with pytest.raises(SafeApiError) as raised:
        parse_expected_version(if_match='W/"7"', body_version=8)
    assert raised.value.status_code == 400
    assert raised.value.code == "EXPECTED_VERSION_CONFLICT"


@pytest.mark.parametrize(
    "value",
    [None, "", "short", "contains space", "!" * 200],
)
def test_idempotency_key_is_required_and_bounded(value: str | None) -> None:
    with pytest.raises(SafeApiError) as raised:
        validate_idempotency_key(value)
    assert raised.value.status_code == 400
    assert raised.value.code == "IDEMPOTENCY_KEY_INVALID"


def test_idempotency_key_accepts_stable_opaque_values() -> None:
    assert validate_idempotency_key("request:01J5A8M9X3-safe") == "request:01J5A8M9X3-safe"
