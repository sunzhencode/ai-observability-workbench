from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import (
    ContinuableSnapshot,
    InMemoryStepStore,
    RunRecord,
    StepEvent,
    ToolEffectRecord,
)

from app.adapters.models.investigator import (
    InvestigatorOutputContractError,
    InvestigatorRecoveryError,
    InvestigatorRuntimeError,
    PydanticInvestigatorProbe,
    PydanticInvestigatorRuntime,
    ReportOutput,
)
from app.application.unified_investigations import (
    InvestigationCanceled,
    MetricToolScopeV2,
    ScopedMetricTools,
)
from app.domains.investigations.provider_catalog import build_provider_profile
from app.application.investigator_runtime import InvestigatorRequest, InvestigatorUsageDelta
from app.domains.investigations.runtime import (
    EvidenceSnapshotV2,
    MetricDescriptorV2,
    MetricObservationV2,
    ProtocolProfile,
    ProviderId,
    ProviderProfile,
    ProviderSupportLevel,
)


class Metrics:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def raise_if_canceled(self) -> None:
        return None

    async def list_metrics(self) -> tuple[MetricDescriptorV2, ...]:
        self.calls.append("list_metrics")
        return (MetricDescriptorV2("checkout_error_ratio", "结账错误率", "HTTP 错误比例", "ratio"),)

    async def describe_metric(self, metric_id: str) -> MetricDescriptorV2:
        self.calls.append(f"describe_metric:{metric_id}")
        return MetricDescriptorV2(metric_id, "结账错误率", "HTTP 错误比例", "ratio")

    async def query_metric(self, metric_id: str, window_minutes: int) -> MetricObservationV2:
        self.calls.append(f"query_metric:{metric_id}:{window_minutes}")
        return MetricObservationV2("metric-live", metric_id, "DATA", {"latest": 0.08}, ((1.0, "0.08"),))


class CancelAfterToolMetrics(Metrics):
    def __init__(self) -> None:
        super().__init__()
        self.canceled = False

    def raise_if_canceled(self) -> None:
        if self.canceled:
            raise InvestigationCanceled

    async def list_metrics(self) -> tuple[MetricDescriptorV2, ...]:
        result = await super().list_metrics()
        if self.calls.count("list_metrics") > 1:
            self.canceled = True
        return result


@pytest.mark.asyncio
async def test_metric_tool_scope_keeps_same_metric_plans_separate() -> None:
    expressions: list[str] = []

    class Reader:
        async def query_range(self, expression, *_args):
            expressions.append(expression)
            return {"resultType": "matrix", "result": []}

    tools = ScopedMetricTools(
        investigation_id="scope-test",
        reader=Reader(),
        scope=(
            MetricToolScopeV2(
                "request_errors_total",
                "接口 A 错误",
                "接口 A",
                "",
                'request_errors_total{route="a"}',
                "scope-a",
                "alert-a",
            ),
            MetricToolScopeV2(
                "request_errors_total",
                "接口 B 错误",
                "接口 B",
                "",
                'request_errors_total{route="b"}',
                "scope-b",
                "alert-b",
            ),
        ),
        now=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc),
    )

    first = await tools.query_metric("scope-a", 15)
    second = await tools.query_metric("scope-b", 15)

    assert expressions == [
        'request_errors_total{route="a"}',
        'request_errors_total{route="b"}',
    ]
    assert first.metric_id == second.metric_id == "request_errors_total"
    assert first.evidence_id != second.evidence_id


@pytest.mark.asyncio
async def test_metric_tool_does_not_checkpoint_result_after_cancellation() -> None:
    canceled = False
    observations: list[MetricObservationV2] = []

    class Reader:
        async def query_range(self, *_args):
            nonlocal canceled
            canceled = True
            return {"resultType": "matrix", "result": []}

    tools = ScopedMetricTools(
        investigation_id="cancel-after-read",
        reader=Reader(),
        scope=(
            MetricToolScopeV2(
                "request_errors_total",
                "错误数",
                "只读测试指标",
                "count",
                "request_errors_total",
                "scope-a",
            ),
        ),
        observation_sink=lambda observation, _observed_at: observations.append(
            observation
        ),
        cancel_requested=lambda: canceled,
    )

    with pytest.raises(InvestigationCanceled):
        await tools.query_metric("scope-a", 15)

    assert observations == []
    assert tools.observations == ()


def _request(*, degraded_domains: tuple[str, ...] = ()) -> InvestigatorRequest:
    return InvestigatorRequest(
        snapshot=EvidenceSnapshotV2(
            investigation_id="test-1",
            occurrence_id=18,
            alert_evidence=(
                {
                    "evidence_id": "alert-1",
                    "alertname": "LocalHighErrorRate",
                    "summary": "本地错误率偏高",
                },
            ),
            metric_evidence=(
                MetricObservationV2(
                    "metric-1",
                    "checkout_error_ratio",
                    "DATA",
                    {"latest": 0.08, "minimum": 0.08, "maximum": 0.08},
                    ((1.0, "0.08"), (2.0, "0.08")),
                ),
            ),
            degraded_domains=degraded_domains,
        ),
        provider_profile=ProviderProfile(
            ProviderId.OPENAI,
            ProtocolProfile.RESPONSES,
            ProviderSupportLevel.REVIEWED,
            "https://api.openai.com/v1",
            "synthetic",
        ),
        api_key="synthetic-not-a-real-key",
    )


@pytest.mark.asyncio
async def test_test_model_uses_same_runtime_tools_typed_output_and_step_journal(tmp_path: Path) -> None:
    model = TestModel(
        call_tools=["list_metrics"],
        custom_output_args={
            "summary_zh": "错误率指标在窗口内保持非零，现有证据支持事件仍需人工核对。",
            "verdict": "LIKELY_INCIDENT",
            "confidence": 0.7,
            "findings": [
                {
                    "title_zh": "错误率持续非零",
                    "analysis_zh": "已有指标样本支持告警条件仍然存在。",
                    "evidence_ids": ["metric-1"],
                }
            ],
            "recommended_actions": [
                {
                    "title_zh": "核对接口错误分布",
                    "rationale_zh": "先确定影响范围，再由人工决定处置。",
                    "evidence_ids": ["metric-1"],
                }
            ],
            "missing_evidence_zh": ["缺少同窗口的接口级状态码分布。"],
            "evidence_gain": 1,
        },
    )
    runtime = PydanticInvestigatorRuntime(
        model_override=model,
        journal_path=tmp_path / "harness.db",
    )
    tools = Metrics()

    result = await runtime.run(_request(), tools=tools)

    assert result.report.summary_zh.startswith("错误率指标")
    assert tools.calls.count("list_metrics") == 2  # scope freeze + model tool call
    assert result.tool_call_count == 1
    assert result.request_count == 2
    assert result.activities[0].kind == "run_started"
    assert result.activities[-1].kind == "run_completed"
    assert (tmp_path / "harness.db").exists()
    assert {tool.name for tool in model.last_model_request_parameters.function_tools} == {
        "list_metrics",
        "describe_metric",
        "query_metric",
    }


@pytest.mark.asyncio
async def test_runtime_copies_frozen_degraded_domains_into_the_report(tmp_path: Path) -> None:
    model = TestModel(
        custom_output_args={
            "summary_zh": "已取得指标事实，但服务映射缺失，当前只能形成受限结论。",
            "verdict": "INCONCLUSIVE",
            "confidence": 0.4,
            "findings": [],
            "recommended_actions": [],
            "missing_evidence_zh": ["缺少服务映射。"],
            "evidence_gain": 1,
        },
    )
    runtime = PydanticInvestigatorRuntime(
        model_override=model,
        journal_path=tmp_path / "harness.db",
    )

    result = await runtime.run(
        _request(degraded_domains=("service",)),
        tools=Metrics(),
    )

    assert result.report.degraded_domains == ("service",)


@pytest.mark.asyncio
async def test_runtime_checks_cancellation_before_every_model_request() -> None:
    calls = 0

    def model_function(_messages, agent_info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("list_metrics", {}, "list-before-cancel")]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    agent_info.output_tools[0].name,
                    {
                        "summary_zh": "不应生成这份调查结论。",
                        "verdict": "INCONCLUSIVE",
                        "confidence": 0.1,
                        "findings": [],
                        "recommended_actions": [],
                        "missing_evidence_zh": [],
                        "evidence_gain": 0,
                    },
                    "output-after-cancel",
                )
            ]
        )

    runtime = PydanticInvestigatorRuntime(
        model_override=FunctionModel(model_function)
    )

    with pytest.raises(InvestigationCanceled):
        await runtime.run(_request(), tools=CancelAfterToolMetrics())

    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_records_returned_model_usage_before_late_cancellation() -> None:
    canceled = False
    deltas: list[InvestigatorUsageDelta] = []

    class CancelAfterUsageMetrics(Metrics):
        def raise_if_canceled(self) -> None:
            if canceled:
                raise InvestigationCanceled

    def record_usage(delta: InvestigatorUsageDelta) -> None:
        nonlocal canceled
        deltas.append(delta)
        canceled = True

    def model_function(_messages, agent_info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    agent_info.output_tools[0].name,
                    {
                        "summary_zh": "这份迟到结论不应被采用。",
                        "verdict": "INCONCLUSIVE",
                        "confidence": 0.1,
                        "findings": [],
                        "recommended_actions": [],
                        "missing_evidence_zh": [],
                        "evidence_gain": 0,
                    },
                    "late-output",
                )
            ],
            usage=RequestUsage(input_tokens=17, output_tokens=9),
        )

    runtime = PydanticInvestigatorRuntime(model_override=FunctionModel(model_function))

    with pytest.raises(InvestigationCanceled):
        await runtime.run(
            replace(_request(), usage_sink=record_usage),
            tools=CancelAfterUsageMetrics(),
        )

    assert deltas == [
        InvestigatorUsageDelta(
            request_count=1,
            tool_call_count=1,
            input_tokens=17,
            output_tokens=9,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (type("RateLimitError", (RuntimeError,), {}), "MODEL_RATE_LIMITED"),
        (type("UnknownProviderError", (RuntimeError,), {}), "MODEL_SERVICE_UNAVAILABLE"),
    ],
)
async def test_runtime_reduces_provider_failures_to_stable_safe_codes(
    error_type: type[RuntimeError], expected: str
) -> None:
    def model_function(_messages, _agent_info):
        raise error_type("https://secret-provider.example/v1?tenant=private")

    runtime = PydanticInvestigatorRuntime(model_override=FunctionModel(model_function))

    with pytest.raises(InvestigatorRuntimeError) as caught:
        await runtime.run(_request(), tools=Metrics())

    assert caught.value.code == expected
    assert "secret-provider" not in str(caught.value)


def test_snapshot_v2_has_no_l3_or_raw_transport_fields() -> None:
    fields = set(EvidenceSnapshotV2.__dataclass_fields__)
    assert "l3_series" not in fields
    assert "raw_payload" not in fields
    assert "generator_url" not in fields


def test_model_output_does_not_ask_model_to_decide_degraded_domains() -> None:
    schema = ReportOutput.model_json_schema()

    assert "degraded_domains" not in schema["properties"]


def test_model_output_only_offers_advisory_verdicts() -> None:
    schema = ReportOutput.model_json_schema()
    verdict_schema = schema["properties"]["verdict"]

    assert verdict_schema["enum"] == [
        "LIKELY_INCIDENT",
        "INCONCLUSIVE",
        "NO_INCIDENT_EVIDENCE",
    ]


@pytest.mark.asyncio
async def test_invalid_provider_verdict_returns_a_stable_contract_code() -> None:
    def invalid_verdict(_messages, agent_info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    agent_info.output_tools[0].name,
                    {
                        "summary_zh": "告警条件与指标读数一致。",
                        "verdict": "INCIDENT_CONFIRMED",
                        "confidence": 0.95,
                        "findings": [],
                        "recommended_actions": [],
                        "missing_evidence_zh": [],
                        "evidence_gain": 1,
                    },
                    "invalid-output",
                )
            ]
        )

    runtime = PydanticInvestigatorRuntime(model_override=FunctionModel(invalid_verdict))

    with pytest.raises(InvestigatorOutputContractError) as error:
        await runtime.run(_request(), tools=Metrics())

    assert error.value.code == "MODEL_OUTPUT_CONTRACT_INVALID"


@pytest.mark.asyncio
async def test_remote_probe_uses_the_same_runtime_tool_and_output_contract() -> None:
    calls = 0

    def model_function(_messages, agent_info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_metric",
                        {"metric_id": "synthetic_metric", "window_minutes": 15},
                        "probe-query",
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    agent_info.output_tools[0].name,
                    {
                        "summary_zh": "已取得合成指标，连接契约测试成功。",
                        "verdict": "INCONCLUSIVE",
                        "confidence": 0.5,
                        "findings": [
                            {
                                "title_zh": "已读取合成指标",
                                "analysis_zh": "结构化工具调用与结果解析正常。",
                                "evidence_ids": ["probe-metric-1"],
                            }
                        ],
                        "recommended_actions": [],
                        "missing_evidence_zh": [],
                        "evidence_gain": 1,
                    },
                    "probe-output",
                )
            ]
        )

    runtime = PydanticInvestigatorRuntime(model_override=FunctionModel(model_function))
    probe = PydanticInvestigatorProbe(runtime=runtime)
    result = await probe.test(
        profile=build_provider_profile(ProviderId.OPENAI, model_id="gpt-test"),
        api_key="synthetic-key",
    )

    assert result.ok is True
    assert result.safe_error_code == "OK"
    assert calls == 2


@pytest.mark.asyncio
async def test_runtime_resumes_only_after_a_durable_read_only_tool_boundary() -> None:
    store = InMemoryStepStore()
    prior_run_id = "investigation-test-1-attempt-1"
    conversation_id = "investigation-test-1"
    await store.register_run(
        RunRecord(
            run_id=prior_run_id,
            conversation_id=conversation_id,
            agent_name="incident-investigator",
        )
    )
    for kind in (
        "run_started",
        "model_request_started",
        "model_request_completed",
        "tool_call_started",
        "tool_call_completed",
    ):
        await store.append_event(
            StepEvent(
                run_id=prior_run_id,
                kind=kind,
                step_index=1,
                conversation_id=conversation_id,
                agent_name="incident-investigator",
                tool_call_id="read-1" if kind.startswith("tool_call") else None,
                tool_name="query_metric" if kind.startswith("tool_call") else None,
            )
        )
    await store.record_tool_effect(
        ToolEffectRecord(
            run_id=prior_run_id,
            tool_call_id="read-1",
            tool_name="query_metric",
            status="completed",
        )
    )
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=prior_run_id,
            step_index=1,
            conversation_id=conversation_id,
            agent_name="incident-investigator",
            messages=[
                ModelRequest(parts=[UserPromptPart("调查")]),
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            "query_metric",
                            {
                                "metric_id": "checkout_error_ratio",
                                "window_minutes": 15,
                            },
                            "read-1",
                        )
                    ]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            "query_metric",
                            {
                                "evidence_id": "metric-resumed",
                                "metric_id": "checkout_error_ratio",
                                "status": "DATA",
                                "summary": {"latest": 0.08},
                                "sample": [[1.0, "0.08"]],
                            },
                            "read-1",
                        )
                    ]
                ),
            ],
        )
    )
    calls = 0

    def finish(_messages, agent_info):
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    agent_info.output_tools[0].name,
                    {
                        "summary_zh": "已从持久只读边界恢复并形成结论。",
                        "verdict": "LIKELY_INCIDENT",
                        "confidence": 0.7,
                        "findings": [
                            {
                                "title_zh": "错误率持续非零",
                                "analysis_zh": "恢复前取得的指标证据仍然有效。",
                                "evidence_ids": ["metric-resumed"],
                            }
                        ],
                        "recommended_actions": [],
                        "missing_evidence_zh": [],
                        "evidence_gain": 1,
                    },
                    "final-output",
                )
            ]
        )

    runtime = PydanticInvestigatorRuntime(
        model_override=FunctionModel(finish),
        step_store=store,
    )

    result = await runtime.run(_request(), tools=Metrics())

    assert calls == 1
    assert result.run_id == "investigation-test-1-attempt-2"
    assert result.report.findings[0].evidence_ids == ("metric-resumed",)


@pytest.mark.asyncio
async def test_runtime_never_retries_an_ambiguous_paid_model_request() -> None:
    store = InMemoryStepStore()
    run_id = "investigation-test-1-attempt-1"
    conversation_id = "investigation-test-1"
    await store.register_run(
        RunRecord(
            run_id=run_id,
            conversation_id=conversation_id,
            agent_name="incident-investigator",
        )
    )
    for kind in ("run_started", "model_request_started"):
        await store.append_event(
            StepEvent(
                run_id=run_id,
                kind=kind,
                step_index=0,
                conversation_id=conversation_id,
                agent_name="incident-investigator",
            )
        )
    calls = 0

    def should_not_call(_messages, _agent_info):
        nonlocal calls
        calls += 1
        raise AssertionError("ambiguous paid request must not be replayed")

    runtime = PydanticInvestigatorRuntime(
        model_override=FunctionModel(should_not_call),
        step_store=store,
    )

    with pytest.raises(InvestigatorRecoveryError) as error:
        await runtime.run(_request(), tools=Metrics())

    assert error.value.code == "MODEL_RESULT_UNKNOWN_AFTER_CRASH"
    assert calls == 0
