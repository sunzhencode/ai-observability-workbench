"""OpenAI Responses API adapter for the official OpenAI target.

The workbench's internal model contract predates Responses and deliberately
stays provider-neutral: callers pass messages, Chat-shaped function schemas and
an optional Chat-shaped JSON schema. This adapter performs the protocol
translation at the edge and returns the same ``ModelReply`` used by the rest of
the investigation pipeline.

The request is stateless (``store=False`` and no ``previous_response_id``),
bounded, guarded on every wire request, never redirected and never retried.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from app.providers.egress import Resolver

from .base import ModelCallError, ModelFailureKind, ModelReply, ToolCall
from .openai_compatible import (
    MODEL_MAX_OUTPUT_TOKENS,
    OpenAICompatibleConfig,
    _build_client,
    _classify,
)

MODEL_MAX_ANALYST_OUTPUT_TOKENS = 8_192


def uses_responses_api(base_url: str) -> bool:
    """Return whether this is the reviewed official OpenAI API target.

    A hostname substring is intentionally insufficient: a custom gateway named
    ``openai.example`` must retain the compatible protocol it declared.
    """

    parsed = urlsplit(str(base_url or "").strip())
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.openai.com"
        and (parsed.port or 443) == 443
        and parsed.path.rstrip("/") == "/v1"
        and not parsed.query
        and not parsed.fragment
    )


def _responses_tools(
    tools: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for item in tools:
        if item.get("type") != "function" or not isinstance(
            item.get("function"), Mapping
        ):
            raise ModelCallError(ModelFailureKind.NOT_CONFIGURED, "TOOL_SCHEMA_INVALID")
        function = item["function"]
        name = str(function.get("name") or "")
        parameters = function.get("parameters")
        if not name or not isinstance(parameters, Mapping):
            raise ModelCallError(ModelFailureKind.NOT_CONFIGURED, "TOOL_SCHEMA_INVALID")
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": dict(parameters),
                "strict": True,
            }
        )
    return converted


def _responses_text(response_format: Mapping[str, Any]) -> dict[str, Any]:
    if response_format.get("type") != "json_schema" or not isinstance(
        response_format.get("json_schema"), Mapping
    ):
        raise ModelCallError(
            ModelFailureKind.NOT_CONFIGURED, "RESPONSE_SCHEMA_INVALID"
        )
    schema = response_format["json_schema"]
    name = str(schema.get("name") or "")
    value = schema.get("schema")
    if not name or not isinstance(value, Mapping):
        raise ModelCallError(
            ModelFailureKind.NOT_CONFIGURED, "RESPONSE_SCHEMA_INVALID"
        )
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "strict": bool(schema.get("strict", True)),
            "schema": dict(value),
        }
    }


def _responses_tool_calls(response: Any) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for item in getattr(response, "output", None) or ():
        if getattr(item, "type", "") != "function_call":
            continue
        name = str(getattr(item, "name", "") or "")
        if not name:
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)
        try:
            arguments = json.loads(getattr(item, "arguments", "") or "{}")
        except ValueError:
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE) from None
        if not isinstance(arguments, dict):
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)
        calls.append(
            ToolCall(
                call_id=str(getattr(item, "call_id", "") or name),
                name=name,
                arguments=arguments,
            )
        )
    return tuple(calls)


class OpenAIResponsesClient:
    """One stateless official OpenAI Responses call per invocation."""

    kind = "OPENAI_COMPATIBLE"

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        if not uses_responses_api(config.base_url):
            raise ValueError("official OpenAI base URL required")
        self.config = config
        self.transport = transport
        self.resolver = resolver

    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        response_format: Mapping[str, Any] | None = None,
    ) -> ModelReply:
        if not str(self.config.api_key or "").strip():
            raise ModelCallError(ModelFailureKind.NOT_CONFIGURED, "API_KEY_REQUIRED")
        if not str(self.config.model or "").strip():
            raise ModelCallError(ModelFailureKind.NOT_CONFIGURED, "MODEL_REQUIRED")

        request: dict[str, Any] = {
            "model": self.config.model,
            "input": list(messages),
            "max_output_tokens": (
                MODEL_MAX_ANALYST_OUTPUT_TOKENS
                if response_format is not None
                else MODEL_MAX_OUTPUT_TOKENS
            ),
            "parallel_tool_calls": False,
            "store": False,
        }
        if tools:
            request["tools"] = _responses_tools(tools)
        if response_format is not None:
            request["text"] = _responses_text(response_format)

        client = _build_client(
            self.config, transport=self.transport, resolver=self.resolver
        )
        try:
            response = await client.responses.create(**request)
        except Exception as exc:  # noqa: BLE001 - safe classification only
            raise _classify(exc) from None
        finally:
            await client.close()

        if getattr(response, "status", "") == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = str(getattr(details, "reason", "") or "")
            kind = (
                ModelFailureKind.RESPONSE_TOO_LARGE
                if reason == "max_output_tokens"
                else ModelFailureKind.MALFORMED_RESPONSE
            )
            raise ModelCallError(kind, reason.upper())
        text = str(getattr(response, "output_text", "") or "")
        tool_calls = _responses_tool_calls(response)
        if not text and not tool_calls:
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)
        usage = getattr(response, "usage", None)
        return ModelReply(
            text=text,
            tool_calls=tool_calls,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model_name=str(getattr(response, "model", "") or ""),
        )


__all__ = [
    "MODEL_MAX_ANALYST_OUTPUT_TOKENS",
    "OpenAIResponsesClient",
    "uses_responses_api",
]
