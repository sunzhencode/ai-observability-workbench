"""Safe HTTP errors which never serialize raw exceptions or request inputs."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.platform.redaction import redact_sensitive
from app.platform.request_context import REQUEST_ID_HEADER, current_request_id

logger = logging.getLogger("incident_operations.platform.errors")


class SafeApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else current_request_id()


def _body(
    *, request_id: str, code: str, message: str, details: dict[str, Any]
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": redact_sensitive(details),
        }
    }


async def safe_api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, SafeApiError):
        return await safe_unexpected_error_handler(_request, exc)
    request_id = _request_id(_request)
    return JSONResponse(
        status_code=exc.status_code,
        content=_body(
            request_id=request_id,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        ),
        headers={REQUEST_ID_HEADER: request_id},
    )


async def safe_validation_error_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        return await safe_unexpected_error_handler(_request, exc)
    errors: list[dict[str, Any]] = []
    for raw in exc.errors():
        errors.append(
            {key: value for key, value in raw.items() if key not in {"input", "ctx"}}
        )
    request_id = _request_id(_request)
    return JSONResponse(
        status_code=422,
        content=_body(
            request_id=request_id,
            code="REQUEST_VALIDATION_FAILED",
            message="请求字段未通过校验",
            details={"errors": errors},
        ),
        headers={REQUEST_ID_HEADER: request_id},
    )


async def safe_unexpected_error_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    request_id = _request_id(_request)
    logger.error(
        json.dumps(
            {
                "event": "http_request_failed",
                "request_id": request_id,
                "error_type": type(exc).__name__,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return JSONResponse(
        status_code=500,
        content=_body(
            request_id=request_id,
            code="INTERNAL_ERROR",
            message="服务未能完成请求，请使用 request_id 查询本地日志",
            details={},
        ),
        headers={REQUEST_ID_HEADER: request_id},
    )
