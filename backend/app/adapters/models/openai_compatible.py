"""Official-SDK model probe with a per-request public-HTTPS egress guard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import json
import socket
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI

from app.domains.investigations.analyst import (
    AnalystAlertV1,
    AnalystContractError,
    AnalystMetricEvidenceV1,
    AnalystModelRequest,
    analyst_messages,
    analyst_response_format,
    available_evidence_ids,
    validate_analyst_result,
)
from app.domains.investigations.planner import (
    InvestigationBudgetV1,
    PlannerAlertV1,
    PlannerModelRequest,
    PlannerReplyV1,
    PlannerToolCallV1,
    parse_planner_decision,
    planner_messages,
    planner_tool_schemas,
)
from app.platform.model_protocols import chat_completion_extra_body

Resolver = Callable[[str], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ModelEgressTarget:
    host: str
    port: int
    addresses: tuple[str, ...]


def _default_resolver(host: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(str(item[4][0]) for item in socket.getaddrinfo(host, 443))
    )


def _public(address: str) -> bool:
    value = ipaddress.ip_address(address)
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None:
        value = value.ipv4_mapped
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_reserved
        or value.is_multicast
        or value.is_unspecified
    )


def assert_model_egress(
    url: str, *, resolver: Resolver | None = None
) -> ModelEgressTarget:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MODEL_EGRESS_HTTPS_REQUIRED")
    port = parsed.port or 443
    if port not in {443, 8443}:
        raise ValueError("MODEL_EGRESS_PORT_REJECTED")
    addresses = tuple(dict.fromkeys((resolver or _default_resolver)(parsed.hostname)))
    if not addresses:
        raise ValueError("MODEL_EGRESS_DNS_EMPTY")
    if not all(_public(item) for item in addresses):
        raise ValueError("MODEL_EGRESS_PRIVATE_ADDRESS")
    return ModelEgressTarget(parsed.hostname, port, addresses)


class _GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        resolver: Resolver | None,
    ) -> None:
        self._inner = inner
        self._resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            assert_model_egress(str(request.url), resolver=self._resolver)
        except ValueError as exc:
            raise httpx.ConnectError(str(exc), request=request) from None
        return await self._inner.handle_async_request(request)


@dataclass(frozen=True, slots=True)
class ModelProbeResult:
    ok: bool
    safe_error_code: str


def _planner_probe_request() -> PlannerModelRequest:
    return PlannerModelRequest(
        investigation_id="synthetic-investigation",
        catalog_revision="synthetic-catalog-1",
        playbook_revision=1,
        available_metric_names=("synthetic_metric",),
        alerts=(
            PlannerAlertV1(
                alert_ref="synthetic-alert-1",
                alertname="SyntheticModelContract",
                severity="info",
                source_state="FIRING",
                labels={"service": "synthetic"},
            ),
        ),
        metric_facts=(),
        empty_facts=(),
        described_metrics=(),
        previous_steps=(),
        budget=InvestigationBudgetV1(),
    )


def _analyst_probe_request() -> AnalystModelRequest:
    return AnalystModelRequest(
        investigation_id="synthetic-investigation",
        snapshot_revision=1,
        prompt_profile_revision=1,
        playbook_revision=1,
        alerts=(
            AnalystAlertV1(
                alert_ref="synthetic-alert-1",
                alertname="SyntheticModelContract",
                severity="info",
                source_state="FIRING",
                labels={"service": "synthetic"},
                annotations={
                    "summary": "Synthetic configuration test; no real alert data."
                },
                starts_at="2026-01-01T00:00:00Z",
                last_seen_at="2026-01-01T00:05:00Z",
            ),
        ),
        alert_summaries=(),
        metric_evidence=(
            AnalystMetricEvidenceV1(
                evidence_ref="synthetic_metric_001",
                alert_ref="synthetic-alert-1",
                metric_name="synthetic_metric",
                l1_summary={"latest": 1.0, "minimum": 0.5, "maximum": 1.0},
                l2_sample=((1.0, "0.5"), (2.0, "1.0")),
            ),
        ),
        empty_evidence=(),
        similar_history=(),
        notes=(),
        degraded_domains=(),
    )


def _planner_reply(message: Any, usage: Any) -> PlannerReplyV1:
    calls: list[PlannerToolCallV1] = []
    for item in getattr(message, "tool_calls", None) or ():
        function = getattr(item, "function", None)
        try:
            arguments = json.loads(getattr(function, "arguments", "") or "{}")
        except ValueError:
            raise ValueError("MODEL_PLANNER_CONTRACT_INVALID") from None
        if not isinstance(arguments, dict):
            raise ValueError("MODEL_PLANNER_CONTRACT_INVALID")
        calls.append(
            PlannerToolCallV1(
                call_id=str(getattr(item, "id", "") or "synthetic-call"),
                name=str(getattr(function, "name", "") or ""),
                arguments=arguments,
            )
        )
    return PlannerReplyV1(
        text=str(getattr(message, "content", "") or ""),
        tool_calls=tuple(calls),
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _uses_responses_api(base_url: str) -> bool:
    parsed = urlsplit(str(base_url or "").strip())
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.openai.com"
        and (parsed.port or 443) == 443
        and parsed.path.rstrip("/") == "/v1"
        and not parsed.query
        and not parsed.fragment
    )


def _responses_tools(tools: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for item in tools:
        function = item.get("function")
        if item.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("MODEL_PLANNER_CONTRACT_INVALID")
        name = str(function.get("name") or "")
        parameters = function.get("parameters")
        if not name or not isinstance(parameters, dict):
            raise ValueError("MODEL_PLANNER_CONTRACT_INVALID")
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": parameters,
                "strict": True,
            }
        )
    return converted


def _responses_text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    schema = response_format.get("json_schema")
    if response_format.get("type") != "json_schema" or not isinstance(schema, dict):
        raise ValueError("MODEL_ANALYST_CONTRACT_INVALID")
    name = str(schema.get("name") or "")
    value = schema.get("schema")
    if not name or not isinstance(value, dict):
        raise ValueError("MODEL_ANALYST_CONTRACT_INVALID")
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "strict": bool(schema.get("strict", True)),
            "schema": value,
        }
    }


def _responses_planner_reply(response: Any) -> PlannerReplyV1:
    calls: list[PlannerToolCallV1] = []
    for item in getattr(response, "output", None) or ():
        if getattr(item, "type", "") != "function_call":
            continue
        try:
            arguments = json.loads(getattr(item, "arguments", "") or "{}")
        except ValueError:
            raise ValueError("MODEL_PLANNER_CONTRACT_INVALID") from None
        if not isinstance(arguments, dict):
            raise ValueError("MODEL_PLANNER_CONTRACT_INVALID")
        calls.append(
            PlannerToolCallV1(
                call_id=str(getattr(item, "call_id", "") or "synthetic-call"),
                name=str(getattr(item, "name", "") or ""),
                arguments=arguments,
            )
        )
    usage = getattr(response, "usage", None)
    return PlannerReplyV1(
        text=str(getattr(response, "output_text", "") or ""),
        tool_calls=tuple(calls),
        prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


class OpenAICompatibleProbe:
    """Synthetic Planner and Analyst contract calls; no retry and no redirect."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._transport = transport
        self._resolver = resolver

    async def test(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
    ) -> ModelProbeResult:
        if not model.strip() or not api_key.strip():
            return ModelProbeResult(False, "MODEL_CONFIGURATION_INCOMPLETE")
        http_client = httpx.AsyncClient(
            transport=_GuardedTransport(
                self._transport or httpx.AsyncHTTPTransport(),
                resolver=self._resolver,
            ),
            timeout=30.0,
            follow_redirects=False,
        )
        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            # OpenAI 2.x temporarily exposes its vendored httpx2 client in the
            # type surface, while accepting a regular compatible AsyncClient
            # at runtime. Keep the guarded transport and isolate that typing
            # mismatch at this SDK boundary.
            http_client=cast(Any, http_client),
            max_retries=0,
        )
        try:
            responses_api = _uses_responses_api(base_url)
            if responses_api:
                responses_planner_parameters: dict[str, Any] = {
                    "model": model,
                    "input": list(planner_messages(_planner_probe_request())),
                    "tools": _responses_tools(planner_tool_schemas()),
                    "max_output_tokens": 256,
                    "parallel_tool_calls": False,
                    "store": False,
                }
                planner_response: Any = await client.responses.create(
                    **responses_planner_parameters
                )
                try:
                    parse_planner_decision(_responses_planner_reply(planner_response))
                except (AttributeError, TypeError, ValueError):
                    return ModelProbeResult(False, "MODEL_PLANNER_CONTRACT_INVALID")
            else:
                planner_parameters: dict[str, Any] = {
                    "model": model,
                    "messages": list(planner_messages(_planner_probe_request())),
                    "tools": list(planner_tool_schemas()),
                    "max_completion_tokens": 256,
                }
                extra_body = chat_completion_extra_body(base_url, model)
                if extra_body:
                    planner_parameters["extra_body"] = extra_body
                planner_response = await client.chat.completions.create(
                    **planner_parameters
                )
                try:
                    planner_message = planner_response.choices[0].message
                    parse_planner_decision(
                        _planner_reply(
                            planner_message, getattr(planner_response, "usage", None)
                        )
                    )
                except (AttributeError, IndexError, TypeError, ValueError):
                    return ModelProbeResult(False, "MODEL_PLANNER_CONTRACT_INVALID")

            analyst_request = _analyst_probe_request()
            if responses_api:
                responses_analyst_parameters: dict[str, Any] = {
                    "model": model,
                    "input": list(analyst_messages(analyst_request)),
                    "max_output_tokens": 1_024,
                    "parallel_tool_calls": False,
                    "store": False,
                    "text": _responses_text_format(analyst_response_format()),
                }
                analyst_response: Any = await client.responses.create(
                    **responses_analyst_parameters
                )
                analyst_content = analyst_response.output_text
            else:
                analyst_parameters: dict[str, Any] = {
                    "model": model,
                    "messages": list(analyst_messages(analyst_request)),
                    "max_completion_tokens": 1_024,
                    "response_format": analyst_response_format(),
                }
                extra_body = chat_completion_extra_body(base_url, model)
                if extra_body:
                    analyst_parameters["extra_body"] = extra_body
                analyst_response = await client.chat.completions.create(
                    **analyst_parameters
                )
                analyst_content = analyst_response.choices[0].message.content
            try:
                if not isinstance(analyst_content, str) or not analyst_content.strip():
                    raise AnalystContractError("ANALYST_CONTRACT_INVALID")
                validate_analyst_result(
                    analyst_content,
                    available_evidence_ids=available_evidence_ids(analyst_request),
                )
            except (AnalystContractError, AttributeError, IndexError, TypeError):
                return ModelProbeResult(False, "MODEL_ANALYST_CONTRACT_INVALID")
            return ModelProbeResult(True, "OK")
        except ValueError as exc:
            code = str(exc)
            return ModelProbeResult(False, code if code.startswith("MODEL_EGRESS_") else "MODEL_REQUEST_REJECTED")
        except httpx.TimeoutException:
            return ModelProbeResult(False, "MODEL_TIMEOUT")
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.ConnectError) and str(cause).startswith(
                "MODEL_EGRESS_"
            ):
                return ModelProbeResult(False, str(cause))
            name = type(exc).__name__
            if name in {"AuthenticationError", "PermissionDeniedError"}:
                return ModelProbeResult(False, "MODEL_AUTH_FAILED")
            if name == "RateLimitError":
                return ModelProbeResult(False, "MODEL_RATE_LIMITED")
            return ModelProbeResult(False, "MODEL_SERVICE_UNAVAILABLE")
        finally:
            await client.close()

    async def list_models(
        self,
        *,
        base_url: str,
        api_key: str,
    ) -> tuple[str, ...]:
        if not api_key.strip():
            return ()
        http_client = httpx.AsyncClient(
            transport=_GuardedTransport(
                self._transport or httpx.AsyncHTTPTransport(),
                resolver=self._resolver,
            ),
            timeout=30.0,
            follow_redirects=False,
        )
        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=cast(Any, http_client),
            max_retries=0,
        )
        try:
            page: Any = await client.models.list()
            names = sorted(
                {
                    str(getattr(item, "id", ""))
                    for item in getattr(page, "data", ())
                    if str(getattr(item, "id", ""))
                }
            )
            return tuple(names[:200])
        finally:
            await client.close()
