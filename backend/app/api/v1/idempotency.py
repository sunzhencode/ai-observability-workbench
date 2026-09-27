"""HTTP boundary for durable idempotent create commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.api.v1.contracts import validate_idempotency_key
from app.application.commands import (
    CommandConflict,
    ExternalOutcomeUnknown,
    IdempotentCommands,
)
from app.platform.errors import SafeApiError


def execute_idempotent_create(
    commands: IdempotentCommands,
    *,
    scope: str,
    key: str,
    payload: Mapping[str, Any],
    action: Callable[[], Mapping[str, Any]],
) -> Mapping[str, Any]:
    validated = validate_idempotency_key(key)
    try:
        return commands.execute(
            scope=scope,
            key=validated,
            payload=payload,
            action=action,
        )
    except CommandConflict as exc:
        if exc.code == "IDEMPOTENCY_KEY_REUSED":
            raise SafeApiError(
                status_code=409,
                code=exc.code,
                message="该 Idempotency-Key 已用于不同请求",
            ) from exc
        raise SafeApiError(
            status_code=409,
            code=exc.code,
            message="前次命令结果不确定，系统不会自动重复执行",
        ) from exc


async def execute_idempotent_external(
    commands: IdempotentCommands,
    *,
    scope: str,
    key: str,
    payload: Mapping[str, Any],
    action: Callable[[], Awaitable[Mapping[str, Any]]],
) -> Mapping[str, Any]:
    validated = validate_idempotency_key(key)
    try:
        return await commands.execute_external(
            scope=scope,
            key=validated,
            payload=payload,
            action=action,
        )
    except ExternalOutcomeUnknown as exc:
        raise SafeApiError(
            status_code=409,
            code="COMMAND_OUTCOME_UNKNOWN",
            message="外部命令结果不确定，系统不会自动重复执行",
        ) from exc
    except CommandConflict as exc:
        if exc.code == "IDEMPOTENCY_KEY_REUSED":
            raise SafeApiError(
                status_code=409,
                code=exc.code,
                message="该 Idempotency-Key 已用于不同请求",
            ) from exc
        raise SafeApiError(
            status_code=409,
            code=exc.code,
            message="外部命令结果不确定，系统不会自动重复执行",
        ) from exc
