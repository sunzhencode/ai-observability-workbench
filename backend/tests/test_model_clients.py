"""The model client's safety properties, one test per property.

Two of these are the whole reason this module is separate from the notification
providers, and both are easy to break by accident later:

- the egress guard runs before **every** send, not once at save time;
- **nothing is retried inside the client**, because a timeout or a 5xx may
  already have been billed (D45).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.model.base import NEVER_BILLED, ModelCallError, ModelFailureKind
from app.providers.model.egress import EgressRejected
from app.providers.model.fake import FakeModelClient
from app.providers.model.openai_compatible import (
    MODEL_MAX_OUTPUT_TOKENS,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from app.providers.model.openai_responses import (
    MODEL_MAX_ANALYST_OUTPUT_TOKENS,
    OpenAIResponsesClient,
    uses_responses_api,
)
from app.providers.model.registry import (
    SUPPORTED_MODEL_KINDS,
    UnsupportedModelKind,
    UnsupportedModelMode,
    get_model_client,
)

MESSAGES = [{"role": "user", "content": "hi"}]
PUBLIC = ("93.184.216.34",)


def _config(base_url: str = "https://api.example.com/v1") -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(base_url=base_url, model="gpt-x", api_key="k-secret")


def _client(handler, *, addresses=PUBLIC, base_url: str = "https://api.example.com/v1"):
    return OpenAICompatibleClient(
        _config(base_url),
        transport=httpx.MockTransport(handler),
        resolver=lambda host: list(addresses),
    )


def _responses_client(handler, *, addresses=PUBLIC):
    return OpenAIResponsesClient(
        OpenAICompatibleConfig(
            base_url="https://api.openai.com/v1",
            model="gpt-5.5",
            api_key="k-secret",
        ),
        transport=httpx.MockTransport(handler),
        resolver=lambda host: list(addresses),
    )


def _responses_body(*, text: str = '{"proposals": []}', output: list[dict] | None = None) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-5.5-2026-08-01",
        "output": output
        if output is not None
        else [
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "text": text,
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 11,
            "output_tokens": 3,
            "total_tokens": 14,
        },
    }


def _ok_body(**extra) -> dict:
    body = {
        "model": "gpt-x-2026",
        "choices": [{"message": {"content": '{"proposals": []}'}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    body.update(extra)
    return body


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_ok_body())


# --- the happy path, and what it sends ------------------------------------


@pytest.mark.asyncio
async def test_it_posts_a_bounded_completion_and_reports_usage() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body())

    reply = await _client(handler).complete(messages=MESSAGES)

    assert reply.text == '{"proposals": []}'
    assert (reply.prompt_tokens, reply.completion_tokens) == (11, 3)
    assert reply.model_name == "gpt-x-2026"

    request = seen[0]
    assert request.method == "POST"
    # The base URL's path is preserved, not replaced.
    assert str(request.url).endswith("/v1/chat/completions")
    body = json.loads(request.content)
    # `max_completion_tokens`, not `max_tokens`: the newer reasoning models
    # reject the old name with a 400, and every current model accepts the new
    # one — so sending the old name only costs compatibility.
    assert body["max_completion_tokens"] > 0, "unbounded output is an unbounded bill"
    assert "max_tokens" not in body
    # No temperature at all: reasoning models reject any non-default value, and
    # a knob that turns a working setup into a hard failure is not worth the
    # small drift reduction. Structure is enforced by validation, not sampling.
    assert "temperature" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["kimi-k2.5", "kimi-k2.6"])
async def test_moonshot_kimi_disables_thinking_on_real_chat_completions(
    model: str,
) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body(model=model))

    client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            base_url="https://api.moonshot.cn/v1",
            model=model,
            api_key="k-secret",
        ),
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: list(PUBLIC),
    )
    await client.complete(messages=MESSAGES)

    assert seen[0]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_kimi_named_model_on_custom_gateway_keeps_default_protocol() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body(model="kimi-k2.6"))

    client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            base_url="https://gateway.example.com/v1",
            model="kimi-k2.6",
            api_key="k-secret",
        ),
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: list(PUBLIC),
    )
    await client.complete(messages=MESSAGES)

    assert "thinking" not in seen[0]


@pytest.mark.asyncio
async def test_official_openai_posts_a_stateless_bounded_response_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_responses_body())

    reply = await _responses_client(handler).complete(messages=MESSAGES)

    assert reply.text == '{"proposals": []}'
    assert (reply.prompt_tokens, reply.completion_tokens) == (11, 3)
    assert reply.model_name == "gpt-5.5-2026-08-01"
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url).endswith("/v1/responses")
    body = json.loads(request.content)
    assert body["model"] == "gpt-5.5"
    assert body["input"] == MESSAGES
    assert body["store"] is False
    assert body["parallel_tool_calls"] is False
    assert body["max_output_tokens"] == MODEL_MAX_OUTPUT_TOKENS
    assert "messages" not in body
    assert "max_completion_tokens" not in body
    assert "previous_response_id" not in body


@pytest.mark.asyncio
async def test_responses_converts_chat_tools_and_reads_function_calls() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_responses_body(
                output=[
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_1",
                        "name": "ListMetrics",
                        "arguments": '{"pattern":"http"}',
                    }
                ]
            ),
        )

    reply = await _responses_client(handler).complete(
        messages=MESSAGES,
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "ListMetrics",
                    "description": "List metrics",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["pattern"],
                        "properties": {"pattern": {"type": "string"}},
                    },
                },
            },
        ),
    )

    assert reply.text == ""
    assert reply.tool_calls[0].call_id == "call_1"
    assert reply.tool_calls[0].name == "ListMetrics"
    assert reply.tool_calls[0].arguments == {"pattern": "http"}
    tool = seen[0]["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "ListMetrics"
    assert tool["parameters"]["additionalProperties"] is False
    assert tool["strict"] is True
    assert "function" not in tool


@pytest.mark.asyncio
async def test_responses_moves_json_schema_under_text_format() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_responses_body(text='{"value":"ok"}'))

    await _responses_client(handler).complete(
        messages=MESSAGES,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "result_v1",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            },
        },
    )

    body = seen[0]
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "result_v1",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
    }
    assert "response_format" not in body
    assert body["max_output_tokens"] == MODEL_MAX_ANALYST_OUTPUT_TOKENS


def test_only_the_official_openai_target_uses_responses() -> None:
    assert uses_responses_api("https://api.openai.com/v1") is True
    assert uses_responses_api("https://api.openai.com/v1/") is True
    assert uses_responses_api("https://api.deepseek.com/v1") is False
    assert uses_responses_api("https://openai.example.com/v1") is False


@pytest.mark.asyncio
async def test_an_incomplete_response_is_not_presented_as_an_empty_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _responses_body(output=[])
        body["status"] = "incomplete"
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        return httpx.Response(200, json=body)

    with pytest.raises(ModelCallError) as excinfo:
        await _responses_client(handler).complete(messages=MESSAGES)
    assert excinfo.value.kind is ModelFailureKind.RESPONSE_TOO_LARGE


@pytest.mark.asyncio
async def test_a_responses_error_is_never_retried_and_keeps_safe_status() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, json={"error": {"message": "nope"}})

    with pytest.raises(ModelCallError) as excinfo:
        await _responses_client(handler).complete(messages=MESSAGES)
    assert attempts == 1
    assert excinfo.value.kind is ModelFailureKind.SERVICE_ERROR
    assert excinfo.value.detail == "HTTP_500"
    assert excinfo.value.possibly_billed is True


@pytest.mark.asyncio
async def test_responses_runs_the_same_resolved_ip_guard_on_the_wire() -> None:
    reached = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reached
        reached += 1
        return httpx.Response(200, json=_responses_body())

    with pytest.raises(ModelCallError) as excinfo:
        await _responses_client(handler, addresses=("127.0.0.1",)).complete(
            messages=MESSAGES
        )
    assert reached == 0
    assert excinfo.value.kind is ModelFailureKind.EGRESS_REJECTED
    assert excinfo.value.possibly_billed is False


@pytest.mark.asyncio
async def test_a_route_prefix_in_the_base_url_survives() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body())

    await _client(handler, base_url="https://api.example.com/gateway/v1/").complete(
        messages=MESSAGES
    )
    assert str(seen[0].url).endswith("/gateway/v1/chat/completions")


@pytest.mark.asyncio
async def test_tool_call_arguments_must_be_a_json_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "list_metrics",
                                        "arguments": '{"substring": "node"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    reply = await _client(handler).complete(messages=MESSAGES)
    assert reply.tool_calls[0].name == "list_metrics"
    assert reply.tool_calls[0].arguments == {"substring": "node"}

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "x", "arguments": "not json"}}
                            ]
                        }
                    }
                ]
            },
        )

    with pytest.raises(ModelCallError) as excinfo:
        await _client(broken).complete(messages=MESSAGES)
    assert excinfo.value.kind is ModelFailureKind.MALFORMED_RESPONSE


# --- the guard runs every time --------------------------------------------


@pytest.mark.asyncio
async def test_the_egress_guard_runs_before_every_send() -> None:
    """A save-time DNS answer is not an egress guarantee; records change."""
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(200, json=_ok_body())

    answers = [["93.184.216.34"], ["127.0.0.1"]]
    client = OpenAICompatibleClient(
        _config(),
        transport=httpx.MockTransport(handler),
        resolver=lambda host: answers.pop(0),
    )

    await client.complete(messages=MESSAGES)
    with pytest.raises(ModelCallError) as excinfo:
        await client.complete(messages=MESSAGES)

    assert excinfo.value.kind is ModelFailureKind.EGRESS_REJECTED
    assert sent == 1, "the second send must never have left"


@pytest.mark.asyncio
async def test_a_rejected_target_is_never_marked_billed() -> None:
    with pytest.raises(ModelCallError) as excinfo:
        await _client(_ok, addresses=("10.0.0.9",)).complete(messages=MESSAGES)
    assert excinfo.value.possibly_billed is False


@pytest.mark.asyncio
async def test_a_redirect_is_not_followed() -> None:
    """Following one would re-target the request at a host never resolved."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.example/v1"})

    with pytest.raises(ModelCallError) as excinfo:
        await _client(handler).complete(messages=MESSAGES)
    assert excinfo.value.kind is ModelFailureKind.SERVICE_ERROR
    assert excinfo.value.possibly_billed is True


# --- nothing retries in here ----------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503])
@pytest.mark.asyncio
async def test_an_error_status_is_sent_exactly_once(status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(ModelCallError) as excinfo:
        await _client(handler).complete(messages=MESSAGES)

    assert attempts == 1, "the client retried a request that may have been billed"
    assert excinfo.value.possibly_billed is True


@pytest.mark.asyncio
async def test_a_timeout_is_sent_once_and_counts_as_possibly_billed() -> None:
    """The tempting one: generation may have started before we gave up."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ModelCallError) as excinfo:
        await _client(handler).complete(messages=MESSAGES)

    assert attempts == 1
    assert excinfo.value.kind is ModelFailureKind.TIMEOUT
    assert ModelFailureKind.TIMEOUT not in NEVER_BILLED


@pytest.mark.asyncio
async def test_a_connect_error_is_the_only_network_failure_known_to_be_free() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ModelCallError) as excinfo:
        await _client(handler).complete(messages=MESSAGES)
    assert excinfo.value.kind is ModelFailureKind.UNREACHABLE
    assert excinfo.value.possibly_billed is False


@pytest.mark.asyncio
async def test_a_reset_mid_request_stays_possibly_billed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset", request=request)

    with pytest.raises(ModelCallError) as excinfo:
        await _client(handler).complete(messages=MESSAGES)
    assert excinfo.value.possibly_billed is True


# --- bounds, and never leaking the address --------------------------------


@pytest.mark.asyncio
async def test_the_output_is_bounded_so_a_runaway_generation_cannot_bill_forever() -> None:
    """The byte caps this replaces were the wrong lever.

    A hand-rolled client had to cap request and response bytes itself. On the
    SDK the meaningful bound is `max_completion_tokens`: it stops a runaway
    generation *at the service*, before it is generated and therefore before it
    is paid for, rather than after it has arrived.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ok_body())

    await _client(handler).complete(messages=MESSAGES)
    body = json.loads(seen[0].content)
    assert body["max_completion_tokens"] == MODEL_MAX_OUTPUT_TOKENS
    assert MODEL_MAX_OUTPUT_TOKENS > 0, "无上限的输出就是无上限的账单"


@pytest.mark.asyncio
async def test_a_reply_with_neither_text_nor_tools_is_a_broken_turn() -> None:
    """Passing "nothing" along makes the caller loop instead of stopping."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {}}]})

    with pytest.raises(ModelCallError) as excinfo:
        await _client(handler).complete(messages=MESSAGES)
    assert excinfo.value.kind is ModelFailureKind.MALFORMED_RESPONSE


@pytest.mark.asyncio
async def test_no_failure_carries_the_address_or_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream said https://api.example.com is down")

    client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            base_url="https://internal-model.corp/v1", model="m", api_key="k-secret"
        ),
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    with pytest.raises(ModelCallError) as excinfo:
        await client.complete(messages=MESSAGES)

    text = str(excinfo.value) + repr(excinfo.value) + excinfo.value.detail
    assert "internal-model" not in text
    assert "k-secret" not in text


def test_the_config_repr_does_not_echo_the_key() -> None:
    assert "k-secret" not in repr(_config())


# --- fake mode, and the registry gate --------------------------------------


@pytest.mark.asyncio
async def test_the_fake_client_holds_no_transport_at_all() -> None:
    """No field to misconfigure into reaching the network."""
    fake = FakeModelClient.from_script("OK")
    assert not hasattr(fake, "transport")

    reply = await fake.complete(messages=MESSAGES)
    assert reply.model_name == "fake-model"
    assert list(fake.calls[0].messages) == MESSAGES


@pytest.mark.asyncio
async def test_a_scripted_failure_stops_rather_than_being_retried() -> None:
    """`HTTP_500,OK` means "500 then stop", not "500 then try again" (D45)."""
    fake = FakeModelClient.from_script("HTTP_500,OK")

    with pytest.raises(ModelCallError) as excinfo:
        await fake.complete(messages=MESSAGES)
    assert excinfo.value.possibly_billed is True
    assert len(fake.calls) == 1, "a second call must be the caller's decision"


def test_an_unrecognised_script_token_fails_closed() -> None:
    fake = FakeModelClient.from_script("WAT")
    assert isinstance(fake.replies[0], ModelCallError)


def test_the_registry_offers_only_the_implemented_security_kind() -> None:
    assert SUPPORTED_MODEL_KINDS == ("OPENAI_COMPATIBLE",)
    with pytest.raises(UnsupportedModelKind):
        get_model_client("LOCAL_LLAMA")


def test_the_registry_selects_responses_only_for_official_openai(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "model_provider_mode", "REAL", raising=False)
    official = get_model_client(
        "OPENAI_COMPATIBLE",
        OpenAICompatibleConfig(
            base_url="https://api.openai.com/v1", model="gpt-5.5", api_key="k"
        ),
    )
    compatible = get_model_client("OPENAI_COMPATIBLE", _config())
    assert isinstance(official, OpenAIResponsesClient)
    assert isinstance(compatible, OpenAICompatibleClient)


def test_mock_mode_forces_the_fake_client_for_every_supported_kind(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "model_provider_mode", "FAKE", raising=False)
    for kind in SUPPORTED_MODEL_KINDS:
        client = get_model_client(kind, _config())
        assert isinstance(client, FakeModelClient)


def test_an_unknown_process_mode_fails_closed_rather_than_defaulting_to_real(
    monkeypatch,
) -> None:
    """"Neither REAL nor FAKE" resolving to REAL would send a real request."""
    from app.config import settings

    monkeypatch.setattr(settings, "model_provider_mode", "MAYBE", raising=False)
    with pytest.raises(UnsupportedModelMode):
        get_model_client("OPENAI_COMPATIBLE", _config())


def test_start_mock_forces_the_model_registry_to_fake() -> None:
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "start.sh"
    text = script.read_text(encoding="utf-8")
    assert 'INCIDENT_OPERATIONS_MODEL_FAKE=1' in text, (
        "start.sh --mock must force FAKE, or a mock run can reach a real model service"
    )


# --- the SDK's defaults are not this repository's rules --------------------


def test_retries_are_switched_off_on_the_sdk_client() -> None:
    """The SDK retries twice by default. That is a doubled bill (D45).

    Asserted on the constructed client rather than by counting requests,
    because the default only shows up on a failure — the shape of test that
    passes for years and then one 429 costs three calls.
    """
    from app.providers.model.openai_compatible import _build_client

    client = _build_client(_config(), transport=None, resolver=None)
    assert client.max_retries == 0


@pytest.mark.asyncio
async def test_the_guard_sits_on_the_wire_not_at_the_call_site() -> None:
    """Every request the SDK makes is checked, including ones we did not write.

    The call-site check this replaced protected exactly the methods someone
    remembered to add it to. `models.list()` is the proof: nobody wired a guard
    into it, and it is guarded anyway.
    """
    reached = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reached
        reached += 1
        return httpx.Response(200, json={"data": []})

    from app.providers.model.openai_compatible import list_models

    with pytest.raises(ModelCallError) as excinfo:
        await list_models(
            _config(),
            transport=httpx.MockTransport(handler),
            resolver=lambda host: ["169.254.169.254"],
        )

    assert excinfo.value.kind is ModelFailureKind.EGRESS_REJECTED
    assert excinfo.value.possibly_billed is False
    assert reached == 0, "护栏之后仍然发出去了"


@pytest.mark.asyncio
async def test_listing_models_returns_ids_from_the_service() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": ""}]},
        )

    from app.providers.model.openai_compatible import list_models

    names = await list_models(
        _config(), transport=httpx.MockTransport(handler), resolver=lambda h: list(PUBLIC)
    )
    assert names == ["gpt-4o", "gpt-4o-mini"]
