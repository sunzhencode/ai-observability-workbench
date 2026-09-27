"""The kind the old boundary existed to prevent, so the guard is the point.

Every test is offline: httpx gets a MockTransport and the resolver is injected.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.generic_webhook import (
    GenericWebhookConfig,
    GenericWebhookProvider,
    build_signature,
)
from app.services.notification_renderer import MessageContext, render_webhook_payload

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def _provider(handler, resolver=PUBLIC) -> GenericWebhookProvider:
    return GenericWebhookProvider(
        transport=httpx.MockTransport(handler), resolver=resolver
    )


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


CONFIG = GenericWebhookConfig(url="https://hooks.example.com/services/abc")


class TestEgressIsCheckedEverySend:
    async def test_a_name_resolving_to_metadata_never_gets_a_request(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request):  # pragma: no cover - must not run
            calls.append(request)
            return _ok(request)

        provider = _provider(handler, resolver=lambda host: ["169.254.169.254"])
        result = await provider.send({}, CONFIG)

        assert result.ok is False
        assert result.code == "EGRESS_PRIVATE_ADDRESS"
        assert calls == []

    async def test_a_record_that_changes_between_sends_is_caught(self) -> None:
        answers = [["93.184.216.34"], ["10.0.0.7"]]
        provider = _provider(_ok, resolver=lambda host: answers.pop(0))

        assert (await provider.send({}, CONFIG)).ok is True
        # Same channel, same config, different DNS answer.
        assert (await provider.send({}, CONFIG)).code == "EGRESS_PRIVATE_ADDRESS"

    async def test_the_error_never_carries_the_target(self) -> None:
        provider = _provider(_ok, resolver=lambda host: ["10.1.2.3"])
        result = await provider.send({}, GenericWebhookConfig(url="https://internal.corp/x"))
        assert "internal.corp" not in str(result)
        assert "10.1.2.3" not in str(result)


class TestRequest:
    async def test_it_posts_json_with_the_configured_headers(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return _ok(request)

        config = GenericWebhookConfig(
            url="https://hooks.example.com/x", headers={"Authorization": "Bearer t"}
        )
        assert (await _provider(handler).send({"a": 1}, config)).ok is True
        request = seen[0]
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["authorization"] == "Bearer t"
        assert json.loads(request.content) == {"a": 1}

    async def test_a_signing_secret_adds_a_verifiable_signature(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return _ok(request)

        config = GenericWebhookConfig(url="https://hooks.example.com/x", signing_secret="s3cret")
        await _provider(handler).send({"a": 1}, config, timestamp=1700000000)
        request = seen[0]
        assert request.headers["x-workbench-timestamp"] == "1700000000"
        assert request.headers["x-workbench-signature"] == build_signature(
            1700000000, request.content, "s3cret"
        )

    async def test_an_oversized_payload_never_leaves(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request):  # pragma: no cover - must not run
            calls.append(request)
            return _ok(request)

        result = await _provider(handler).send({"x": "y" * (300 * 1024)}, CONFIG)
        assert result.code == "PAYLOAD_TOO_LARGE"
        assert calls == []


class TestResponse:
    @pytest.mark.parametrize("status", [301, 302, 307])
    async def test_a_redirect_is_refused_rather_than_followed(self, status: int) -> None:
        provider = _provider(lambda request: httpx.Response(status, headers={"location": "https://elsewhere.example/"}))
        result = await provider.send({}, CONFIG)
        assert result.code == "REDIRECT_REJECTED"
        assert result.transient is False

    async def test_5xx_and_429_are_retried_and_4xx_is_not(self) -> None:
        for status, transient in [(500, True), (503, True), (429, True), (400, False), (404, False)]:
            provider = _provider(lambda request, s=status: httpx.Response(s))
            result = await provider.send({}, CONFIG)
            assert result.ok is False
            assert result.transient is transient, status

    async def test_a_huge_response_body_is_refused(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, content=b"x" * (100 * 1024)))
        assert (await provider.send({}, CONFIG)).code == "RESPONSE_TOO_LARGE"

    async def test_a_2xx_is_success_without_reading_the_body(self) -> None:
        provider = _provider(lambda request: httpx.Response(204, content=b"whatever"))
        assert (await provider.send({}, CONFIG)).ok is True

    async def test_network_failures_are_transient(self) -> None:
        def boom(request):
            raise httpx.ConnectError("refused")

        assert (await _provider(boom).send({}, CONFIG)).transient is True


class TestPayloadContract:
    def _context(self, **overrides) -> MessageContext:
        values = dict(
            event_type="FIRING_OPENED",
            severity="CRITICAL",
            title="TargetDown",
            source="prod",
            source_state="firing",
            occurrence_no=2,
            member_count=9,
            members=[{"alertname": f"A{i}", "severity": "warning"} for i in range(9)],
            group_labels={"cluster": "a"},
            incident_id=42,
            workbench_url="https://wb.example",
        )
        values.update(overrides)
        return MessageContext(**values)

    def test_it_is_versioned_because_it_is_an_outward_contract(self) -> None:
        payload = render_webhook_payload(self._context())
        assert payload["schema_version"] == 1

    def test_it_carries_the_same_facts_as_the_other_kinds(self) -> None:
        payload = render_webhook_payload(self._context())
        assert payload["event_type"] == "FIRING_OPENED"
        assert payload["incident"]["severity"] == "CRITICAL"
        assert payload["incident"]["member_count"] == 9
        assert payload["source"] == "prod"
        assert payload["workbench_url"] == "https://wb.example/incidents/42"

    def test_members_are_capped_the_same_way_a_card_caps_them(self) -> None:
        payload = render_webhook_payload(self._context())
        assert len(payload["incident"]["members"]) == 5
        assert payload["incident"]["member_count"] == 9

    def test_it_is_json_serialisable(self) -> None:
        json.dumps(render_webhook_payload(self._context()))
