"""Reusable HTTP idempotency and optimistic-version guards."""

from __future__ import annotations

import re

from app.platform.errors import SafeApiError

ETAG_PATTERN = re.compile(r'^(?:W/)?"([1-9][0-9]*)"$')
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{12,128}$")


def format_etag(version: int) -> str:
    if version < 1:
        raise ValueError("version must be positive")
    return f'W/"{version}"'


def parse_expected_version(
    *,
    if_match: str | None,
    body_version: int | None,
) -> int:
    header_version: int | None = None
    if if_match is not None:
        match = ETAG_PATTERN.fullmatch(if_match.strip())
        if match is None:
            raise SafeApiError(
                status_code=400,
                code="EXPECTED_VERSION_INVALID",
                message="If-Match 必须包含有效资源版本",
            )
        header_version = int(match.group(1))
    if body_version is not None and body_version < 1:
        raise SafeApiError(
            status_code=400,
            code="EXPECTED_VERSION_INVALID",
            message="expected_version 必须为正整数",
        )
    if header_version is not None and body_version is not None and header_version != body_version:
        raise SafeApiError(
            status_code=400,
            code="EXPECTED_VERSION_CONFLICT",
            message="If-Match 与请求体版本不一致",
        )
    resolved = header_version if header_version is not None else body_version
    if resolved is None:
        raise SafeApiError(
            status_code=428,
            code="EXPECTED_VERSION_REQUIRED",
            message="修改资源必须提供期望版本",
        )
    return resolved


def validate_idempotency_key(value: str | None) -> str:
    if value is None or IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise SafeApiError(
            status_code=400,
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 必须是 12-128 位稳定不透明值",
        )
    return value
