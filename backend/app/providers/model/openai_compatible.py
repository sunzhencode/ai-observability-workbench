"""The one model client the first version implements (D17), on OpenAI's SDK.

Hand-writing the request body is what produced the `max_tokens` bug: OpenAI
deprecated that field in favour of `max_completion_tokens`, and the reasoning
models **reject** the old name with a 400. The official client tracks changes
like that; a hand-rolled body tracks whatever was true the day it was written.

Three of this repository's rules are not the SDK's defaults, so each is set
explicitly and asserted by a test:

- **The egress guard runs on the wire, not at a call site.** It lives in a
  transport wrapper, so every request the SDK makes -- completions, model
  listing, anything added later -- is checked on the *resolved address* before
  it leaves. Stronger than the call-site check it replaces: there is no longer a
  code path that could forget to ask.
- **No retries.** `max_retries=0`; the SDK retries twice by default, and a
  timeout or 5xx may already have been billed (D45).
- **No redirects.** A redirect re-targets the request at a host the guard never
  resolved, which is the guard defeating itself.

Nothing leaving this module carries an address or a response body: `str()` on a
transport error embeds the full URL, which is how an internal hostname reached an
API response once already (`sources/thanos.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

from app.platform.model_protocols import chat_completion_extra_body

from .base import ModelCallError, ModelFailureKind, ModelReply, ToolCall
from .egress import (
    MODEL_KIND_OPENAI_COMPATIBLE,
    EgressRejected,
    Resolver,
    assert_model_egress,
)

MODEL_MAX_OUTPUT_TOKENS = 2048
MODEL_REQUEST_TIMEOUT_SECONDS = 90.0

#: Some gateways answer `/models` with hundreds; a picker is not a catalogue.
MAX_LISTED_MODELS = 200


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    base_url: str
    model: str
    # `repr=False` so a stray log line or traceback frame cannot print it.
    api_key: str = field(repr=False, default="")

    def __post_init__(self) -> None:
        if not str(self.base_url or "").strip():
            raise ValueError("base_url is required")
        # `model` may be empty. Listing what a service offers needs only the
        # address and the key, and demanding all three here is what created a
        # deadlock: the picker needed a saved draft, and saving demanded the
        # very name the picker existed to supply. Each operation states what it
        # needs; `complete` refuses without a model, `list_models` does not care.


class _GuardedTransport(httpx.AsyncBaseTransport):
    """Runs the egress guard on every request, then delegates.

    At this level the guard cannot be routed around: it does not matter which
    SDK method built the request, or whether a future one forgets to ask. It
    resolves per request rather than once, because a save-time DNS answer is not
    a guarantee about the next send.
    """

    def __init__(
        self, inner: httpx.AsyncBaseTransport, *, kind: str, resolver: Resolver | None
    ) -> None:
        self._inner = inner
        self._kind = kind
        self._resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            assert_model_egress(
                str(request.url), kind=self._kind, resolver=self._resolver
            )
        except EgressRejected as exc:
            # Surfaced as a connect error so the SDK classifies it as "never
            # reached the service" -- which is exactly what happened, and what
            # `NEVER_BILLED` depends on being true.
            raise httpx.ConnectError(exc.code, request=request) from None
        return await self._inner.handle_async_request(request)


def _build_client(
    config: OpenAICompatibleConfig,
    *,
    transport: httpx.AsyncBaseTransport | None,
    resolver: Resolver | None,
) -> AsyncOpenAI:
    http_client = httpx.AsyncClient(
        transport=_GuardedTransport(
            transport or httpx.AsyncHTTPTransport(),
            kind=MODEL_KIND_OPENAI_COMPATIBLE,
            resolver=resolver,
        ),
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    return AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        http_client=http_client,
        # D45: a possibly-billed request is never retried on the user's behalf.
        max_retries=0,
    )


def _classify(exc: Exception) -> ModelCallError:
    """Map an SDK failure onto a kind, without letting the address escape."""

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        detail = f"HTTP_{status}"
        if status in {401, 403}:
            return ModelCallError(ModelFailureKind.AUTH_FAILED, detail)
        if status == 429:
            return ModelCallError(ModelFailureKind.RATE_LIMITED, detail)
        return ModelCallError(ModelFailureKind.SERVICE_ERROR, detail)
    if isinstance(exc, APITimeoutError):
        # Includes connect timeouts. Kept in the possibly-billed bucket rather
        # than inferred from timing: generation may already have started (D45).
        return ModelCallError(ModelFailureKind.TIMEOUT)
    if isinstance(exc, APIConnectionError):
        # The SDK collapses every transport failure into this one class, but
        # they are not the same for billing: a refused connection proves nothing
        # was delivered, while a read error or a reset can happen *after* the
        # request landed and generation began. Only the first may be called
        # never-billed (D45), so the cause has to be inspected.
        cause = exc.__cause__
        if isinstance(cause, httpx.ConnectError):
            code = str(cause)
            if code.startswith("EGRESS_"):
                return ModelCallError(ModelFailureKind.EGRESS_REJECTED, code)
            return ModelCallError(ModelFailureKind.UNREACHABLE)
        return ModelCallError(ModelFailureKind.SERVICE_ERROR)
    if isinstance(exc, OpenAIError):
        return ModelCallError(ModelFailureKind.SERVICE_ERROR)
    raise exc


def _tool_calls_from(message: Any) -> tuple[ToolCall, ...]:
    raw = getattr(message, "tool_calls", None) or ()
    calls: list[ToolCall] = []
    for item in raw:
        function = getattr(item, "function", None)
        name = getattr(function, "name", "")
        if not name:
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)
        try:
            arguments = json.loads(getattr(function, "arguments", "") or "{}")
        except ValueError:
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE) from None
        if not isinstance(arguments, dict):
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)
        calls.append(
            ToolCall(
                call_id=str(getattr(item, "id", "") or name),
                name=name,
                arguments=arguments,
            )
        )
    return tuple(calls)


class OpenAICompatibleClient:
    """One chat completion per call. No retries, no streaming, no state."""

    kind = MODEL_KIND_OPENAI_COMPATIBLE

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
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
            "messages": list(messages),
            # `max_completion_tokens`, never `max_tokens`: the latter is
            # deprecated and the reasoning models reject it with a 400.
            "max_completion_tokens": MODEL_MAX_OUTPUT_TOKENS,
        }
        # `temperature` deliberately absent: reasoning models reject any
        # non-default value, and a knob that turns a working configuration into
        # a hard failure is not worth the drift it removes. What makes the answer
        # usable here is the strict schema and the validators, not sampling.
        if tools:
            request["tools"] = list(tools)
        if response_format is not None:
            request["response_format"] = dict(response_format)
        extra_body = chat_completion_extra_body(
            self.config.base_url, self.config.model
        )
        if extra_body:
            request["extra_body"] = extra_body

        client = _build_client(
            self.config, transport=self.transport, resolver=self.resolver
        )
        try:
            completion = await client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - classified, never re-raised raw
            raise _classify(exc) from None
        finally:
            await client.close()

        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)
        message = choices[0].message
        text = getattr(message, "content", "") or ""
        tool_calls = _tool_calls_from(message)
        if not text and not tool_calls:
            # Neither an answer nor a question is a broken turn, not an empty
            # answer -- passing it on as nothing makes the caller loop.
            raise ModelCallError(ModelFailureKind.MALFORMED_RESPONSE)

        usage = getattr(completion, "usage", None)
        return ModelReply(
            text=text,
            tool_calls=tool_calls,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            model_name=str(getattr(completion, "model", "") or ""),
        )


async def list_models(
    config: OpenAICompatibleConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> list[str]:
    """The model IDs this service actually offers.

    Exists so the user picks from a list instead of typing one. A typed name is
    a 404 or a 400 waiting to happen, and the service reports either the same
    way it reports being broken -- so "I mistyped" and "it is down" arrive
    looking identical.

    Not a hardcoded enum: OPENAI_**COMPATIBLE** also talks to services we have
    never heard of, and OpenAI ships new models continuously. Asking is the only
    answer that stays correct.
    """

    client = _build_client(config, transport=transport, resolver=resolver)
    try:
        page = await client.models.list()
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc) from None
    finally:
        await client.close()

    names = sorted(
        {
            item.id
            for item in (getattr(page, "data", None) or [])
            if isinstance(getattr(item, "id", None), str) and item.id
        }
    )
    return names[:MAX_LISTED_MODELS]


__all__ = [
    "MAX_LISTED_MODELS",
    "MODEL_MAX_OUTPUT_TOKENS",
    "MODEL_REQUEST_TIMEOUT_SECONDS",
    "OpenAICompatibleClient",
    "OpenAICompatibleConfig",
    "list_models",
]
