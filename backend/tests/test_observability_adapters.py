"""Monitoring-source and model-egress adapter boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest

from app.adapters.models.openai_compatible import OpenAICompatibleProbe, assert_model_egress
from app.adapters.monitoring.grafana import BoundedGrafanaReader
from app.adapters.monitoring.thanos import BoundedThanosReader

UTC = timezone.utc


async def test_thanos_uses_named_bounded_gets_and_never_follows_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"status": "success", "data": {"resultType": "matrix", "result": []}},
        )

    reader = BoundedThanosReader(
        "http://10.0.0.9:9090", transport=httpx.MockTransport(handler)
    )
    await reader.query_range(
        "up", datetime(2026, 8, 12, tzinfo=UTC), datetime(2026, 8, 12, 0, 5, tzinfo=UTC), 15
    )
    assert [(item.method, item.url.path) for item in requests] == [
        ("GET", "/api/v1/query_range")
    ]


async def test_grafana_private_monitoring_address_is_allowed_and_only_two_reads_exist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"dashboard": {"title": "Kubernetes", "panels": []}})

    reader = BoundedGrafanaReader(
        "http://10.0.0.10:3000", transport=httpx.MockTransport(handler)
    )
    assert await reader.search_dashboards("") == ()
    assert (await reader.get_dashboard("kube-main"))["title"] == "Kubernetes"
    public = {
        name for name in dir(reader) if not name.startswith("_") and callable(getattr(reader, name))
    }
    assert public == {"get_dashboard", "search_dashboards"}


def test_model_egress_rejects_private_or_mixed_dns_and_has_no_bypass() -> None:
    with pytest.raises(ValueError, match="MODEL_EGRESS_PRIVATE_ADDRESS"):
        assert_model_egress(
            "https://models.example.invalid/v1",
            resolver=lambda _host: ("203.0.113.9", "10.0.0.8"),
        )
    target = assert_model_egress(
        "https://models.example.invalid/v1",
        resolver=lambda _host: ("8.8.8.8",),
    )
    assert target.host == "models.example.invalid"


async def test_official_sdk_probe_is_bounded_not_retried_and_guarded_each_send() -> None:
    attempts = 0
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content)
        requests.append(body)
        content = (
            '{"action":"FINISH"}'
            if attempts == 1
            else json.dumps(
                {
                    "summary": "合成证据已取得，当前只支持相关性判断。",
                    "hypotheses": [
                        {
                            "title": "合成指标与告警同时变化",
                            "explanation": "已提供的指标事实只能支持相关性。",
                            "verdict": "SUPPORTED",
                            "supporting_evidence_ids": ["synthetic_metric_001"],
                            "contradicting_evidence_ids": [],
                            "missing_evidence": [],
                        }
                    ],
                    "missing_evidence": [],
                    "recommended_actions": [
                        {
                            "kind": "NEXT_CHECK",
                            "description": "核对合成指标的同一时间窗口。",
                            "risk": "只读检查，不改变系统状态。",
                            "evidence_ids": ["synthetic_metric_001"],
                        }
                    ],
                }
            )
        )
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-compatible",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    answers = [("8.8.8.8",), ("8.8.8.8",), ("10.0.0.8",)]
    probe = OpenAICompatibleProbe(
        transport=httpx.MockTransport(handler), resolver=lambda _host: answers.pop(0)
    )
    first = await probe.test(
        base_url="https://models.example.invalid/v1",
        model="gpt-compatible",
        api_key="test-key",
    )
    second = await probe.test(
        base_url="https://models.example.invalid/v1",
        model="gpt-compatible",
        api_key="test-key",
    )
    assert first.ok is True
    assert second.safe_error_code == "MODEL_EGRESS_PRIVATE_ADDRESS"
    assert attempts == 2
    assert requests[0]["tools"]
    assert "response_format" not in requests[0]
    assert "tools" not in requests[1]
    assert requests[1]["response_format"]


async def test_official_openai_probe_uses_responses_for_both_contracts() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        content = (
            '{"action":"FINISH"}'
            if len(requests) == 1
            else json.dumps(
                {
                    "summary": "合成证据已取得，当前只支持相关性判断。",
                    "hypotheses": [
                        {
                            "title": "合成指标与告警同时变化",
                            "explanation": "已提供的指标事实只能支持相关性。",
                            "verdict": "SUPPORTED",
                            "supporting_evidence_ids": ["synthetic_metric_001"],
                            "contradicting_evidence_ids": [],
                            "missing_evidence": [],
                        }
                    ],
                    "missing_evidence": [],
                    "recommended_actions": [
                        {
                            "kind": "NEXT_CHECK",
                            "description": "核对合成指标的同一时间窗口。",
                            "risk": "只读检查，不改变系统状态。",
                            "evidence_ids": ["synthetic_metric_001"],
                        }
                    ],
                }
            )
        )
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-5.5",
                "output": [
                    {
                        "id": f"msg_{len(requests)}",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "annotations": [],
                                "text": content,
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            },
        )

    probe = OpenAICompatibleProbe(
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: ("8.8.8.8",),
    )
    result = await probe.test(
        base_url="https://api.openai.com/v1",
        model="gpt-5.5",
        api_key="test-key",
    )

    assert result.ok is True
    assert [item[0] for item in requests] == ["/v1/responses", "/v1/responses"]
    planner = requests[0][1]
    analyst = requests[1][1]
    assert planner["store"] is False
    assert planner["parallel_tool_calls"] is False
    assert planner["tools"][0]["name"] == "ListMetrics"
    assert "function" not in planner["tools"][0]
    assert analyst["text"]["format"]["type"] == "json_schema"
    assert "response_format" not in analyst


async def test_official_sdk_probe_distinguishes_planner_and_analyst_contract_failures() -> None:
    def invalid_planner(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-compatible",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": '{}'}, "finish_reason": "stop"}],
            },
        )

    planner = OpenAICompatibleProbe(
        transport=httpx.MockTransport(invalid_planner),
        resolver=lambda _host: ("8.8.8.8",),
    )
    planner_result = await planner.test(
        base_url="https://models.example.invalid/v1",
        model="gpt-compatible",
        api_key="test-key",
    )
    assert planner_result.safe_error_code == "MODEL_PLANNER_CONTRACT_INVALID"

    attempts = 0

    def invalid_analyst(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        content = '{"action":"FINISH"}' if attempts == 1 else '{}'
        return httpx.Response(
            200,
            json={
                "id": f"completion-{attempts}",
                "object": "chat.completion",
                "created": attempts,
                "model": "gpt-compatible",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            },
        )

    analyst = OpenAICompatibleProbe(
        transport=httpx.MockTransport(invalid_analyst),
        resolver=lambda _host: ("8.8.8.8",),
    )
    analyst_result = await analyst.test(
        base_url="https://models.example.invalid/v1",
        model="gpt-compatible",
        api_key="test-key",
    )
    assert analyst_result.safe_error_code == "MODEL_ANALYST_CONTRACT_INVALID"


async def test_moonshot_kimi_probe_disables_thinking_for_both_contract_calls() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body.get("thinking") != {"type": "disabled"}:
            content = "我会先分析已有信息，然后再决定是否调用工具。"
        elif len(requests) == 1:
            content = '{"action":"FINISH"}'
        else:
            content = json.dumps(
                {
                    "summary": "合成证据已取得，当前只支持相关性判断。",
                    "hypotheses": [
                        {
                            "title": "合成指标与告警同时变化",
                            "explanation": "已提供的指标事实只能支持相关性。",
                            "verdict": "SUPPORTED",
                            "supporting_evidence_ids": ["synthetic_metric_001"],
                            "contradicting_evidence_ids": [],
                            "missing_evidence": [],
                        }
                    ],
                    "missing_evidence": [],
                    "recommended_actions": [
                        {
                            "kind": "NEXT_CHECK",
                            "description": "核对合成指标的同一时间窗口。",
                            "risk": "只读检查，不改变系统状态。",
                            "evidence_ids": ["synthetic_metric_001"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return httpx.Response(
            200,
            json={
                "id": f"completion-{len(requests)}",
                "object": "chat.completion",
                "created": len(requests),
                "model": "kimi-k2.6",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    probe = OpenAICompatibleProbe(
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: ("8.8.8.8",),
    )
    result = await probe.test(
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.6",
        api_key="test-key",
    )

    assert result.ok is True
    assert len(requests) == 2
    assert all(item["thinking"] == {"type": "disabled"} for item in requests)


async def test_moonshot_thinking_override_does_not_match_custom_gateway() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 1,
                "model": "kimi-k2.6",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "not a planner decision"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    probe = OpenAICompatibleProbe(
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: ("8.8.8.8",),
    )
    result = await probe.test(
        base_url="https://gateway.example.invalid/v1",
        model="kimi-k2.6",
        api_key="test-key",
    )

    assert result.safe_error_code == "MODEL_PLANNER_CONTRACT_INVALID"
    assert "thinking" not in requests[0]
