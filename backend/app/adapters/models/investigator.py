"""Pydantic AI adapter for the repository-owned Investigator runtime port."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, cast

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.guardrails import GuardrailResult, ToolCallInfo, ToolGuardrail
from pydantic_ai_harness.step_persistence import (
    InMemoryStepStore,
    SqliteStepStore,
    StepPersistence,
    StepStore,
    continue_run,
)
from pydantic_ai_harness.tool_output_limits import Band, ToolOutputLimits, Truncate

from app.adapters.models.openai_compatible import (
    ModelProbeResult,
    OpenAICompatibleProbe,
    Resolver,
    _GuardedTransport,
)
from app.application.investigator_runtime import (
    InvestigationRunLimits,
    InvestigatorRequest,
    InvestigatorResult,
    InvestigatorRuntime,
    InvestigatorUsageDelta,
    ReadOnlyMetricTools,
)
from app.application.unified_investigations import InvestigationCanceled
from app.domains.investigations.provider_catalog import (
    PROVIDER_CATALOG,
    provider_settings,
)
from app.domains.investigations.runtime import (
    EvidenceFindingV2,
    EvidenceSnapshotV2,
    InvestigationActionV2,
    InvestigationActivityV2,
    InvestigationReportV2,
    InvestigationVerdict,
    ProtocolProfile,
    ProviderId,
    ProviderProfile,
    MetricDescriptorV2,
    MetricObservationV2,
    validate_report,
)


class ProviderProfileRegistry:
    """Reviewed defaults; caller-supplied CUSTOM targets remain explicit."""

    def resolve(self, provider_id: ProviderId) -> ProviderProfile:
        definition = PROVIDER_CATALOG[provider_id]
        return ProviderProfile(
            provider_id=provider_id,
            protocol=definition.protocol,
            support_level=definition.support_level,
            base_url=definition.base_url,
        )

    def model_settings(self, provider_id: ProviderId, model_id: str) -> dict[str, object]:
        return provider_settings(provider_id, model_id)


@dataclass(frozen=True, slots=True)
class SecureModel:
    model: Model
    http_client: httpx.AsyncClient


class SecureModelFactory:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None) -> None:
        self._transport = transport
        self._resolver = resolver

    def create(self, profile: ProviderProfile, *, api_key: str) -> SecureModel:
        if not api_key.strip() or not profile.model_id.strip():
            raise ValueError("MODEL_CONFIGURATION_INCOMPLETE")
        if not profile.base_url.strip():
            raise ValueError("MODEL_BASE_URL_REQUIRED")
        http_client = httpx.AsyncClient(
            transport=_GuardedTransport(self._transport or httpx.AsyncHTTPTransport(), resolver=self._resolver),
            timeout=30.0,
            follow_redirects=False,
        )
        client = AsyncOpenAI(
            base_url=profile.base_url,
            api_key=api_key,
            # OpenAI 3 supports legacy httpx at runtime while its public type
            # only names httpx2. Pydantic AI uses the same documented escape.
            http_client=http_client,  # type: ignore[arg-type]
            max_retries=0,
        )
        if profile.provider_id is ProviderId.DEEPSEEK:
            provider: OpenAIProvider | DeepSeekProvider = DeepSeekProvider(openai_client=client)
        else:
            provider = OpenAIProvider(openai_client=client)
        if profile.protocol is ProtocolProfile.RESPONSES:
            model: Model = OpenAIResponsesModel(profile.model_id, provider=provider)
        else:
            model = OpenAIChatModel(profile.model_id, provider=provider)
        return SecureModel(model, http_client)


class FindingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_zh: str
    analysis_zh: str
    evidence_ids: list[str]


class ActionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_zh: str
    rationale_zh: str
    evidence_ids: list[str]


class ReportOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_zh: str
    verdict: Literal[
        "LIKELY_INCIDENT",
        "INCONCLUSIVE",
        "NO_INCIDENT_EVIDENCE",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    findings: list[FindingOutput]
    recommended_actions: list[ActionOutput] = Field(max_length=3)
    missing_evidence_zh: list[str]
    evidence_gain: int = Field(ge=0)


@dataclass(slots=True)
class _Deps:
    tools: ReadOnlyMetricTools
    allowed_metric_ids: set[str]


class InvestigatorRecoveryError(RuntimeError):
    """A prior paid request cannot be replayed safely by a worker lease retry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvestigatorOutputContractError(RuntimeError):
    """Provider output violated the typed advisory-report contract."""

    def __init__(self) -> None:
        super().__init__("MODEL_OUTPUT_CONTRACT_INVALID")
        self.code = "MODEL_OUTPUT_CONTRACT_INVALID"


class InvestigatorRuntimeError(RuntimeError):
    """A provider/runtime failure reduced to a repository-safe stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _CancellationCheckedModel(Model):
    """Checks the durable run state immediately around every paid request."""

    def __init__(
        self,
        inner: Model,
        check: Callable[[], None],
        usage_sink: Callable[[InvestigatorUsageDelta], None] | None,
    ) -> None:
        super().__init__(settings=inner.settings, profile=inner.profile)
        self._inner = inner
        self._check = check
        self._usage_sink = usage_sink

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def system(self) -> str:
        return self._inner.system

    @property
    def profile(self) -> Any:
        return self._inner.profile

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._check()
        response = await self._inner.request(
            messages, model_settings, model_request_parameters
        )
        if self._usage_sink is not None:
            self._usage_sink(
                InvestigatorUsageDelta(
                    request_count=1,
                    tool_call_count=sum(
                        isinstance(part, ToolCallPart) for part in response.parts
                    ),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
            )
        self._check()
        return response

    async def cancel_suspended_response(self, response: ModelResponse) -> None:
        await self._inner.cancel_suspended_response(response)

    def continuation_delay(self, response: ModelResponse) -> float | None:
        return self._inner.continuation_delay(response)


@dataclass(frozen=True, slots=True)
class _RecoveryPlan:
    run_id: str
    parent_run_id: str | None
    message_history: tuple[ModelMessage, ...]
    usage: RunUsage


def _history_evidence_ids(messages: tuple[ModelMessage, ...]) -> set[str]:
    evidence_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart) or not isinstance(part.content, dict):
                continue
            evidence_id = part.content.get("evidence_id")
            if evidence_id:
                evidence_ids.add(str(evidence_id))
    return evidence_ids


def _history_tool_call_count(messages: tuple[ModelMessage, ...]) -> int:
    return sum(
        1
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
        and part.tool_name in {"list_metrics", "describe_metric", "query_metric"}
    )


async def _recovery_plan(store: StepStore, *, conversation_id: str) -> _RecoveryPlan:
    runs = sorted(
        await store.list_runs(conversation_id=conversation_id),
        key=lambda item: (item.started_at, item.run_id),
    )
    if not runs:
        return _RecoveryPlan(
            run_id=f"{conversation_id}-attempt-1",
            parent_run_id=None,
            message_history=(),
            usage=RunUsage(),
        )

    latest = runs[-1]
    events = await store.list_events(run_id=latest.run_id)
    kinds = [event.kind for event in events]
    started = kinds.count("model_request_started")
    completed = kinds.count("model_request_completed")
    failed = kinds.count("model_request_failed")
    if failed or "run_failed" in kinds:
        raise InvestigatorRecoveryError("MODEL_RETRY_REQUIRES_OPERATOR")
    if started > completed + failed:
        raise InvestigatorRecoveryError("MODEL_RESULT_UNKNOWN_AFTER_CRASH")

    message_history: tuple[ModelMessage, ...] = ()
    usage = RunUsage()
    if started:
        # Only a completed read-only tool boundary is safe to continue. A
        # completed provider response without such a boundary may already be
        # a paid final answer that the worker died before committing.
        if not kinds or kinds[-1] != "tool_call_completed":
            raise InvestigatorRecoveryError("MODEL_RESULT_UNKNOWN_AFTER_CRASH")
        unresolved = await store.list_unresolved_tool_effects(run_id=latest.run_id)
        if unresolved:
            raise InvestigatorRecoveryError("READ_ONLY_TOOL_RESULT_UNKNOWN_AFTER_CRASH")
        message_history = tuple(await continue_run(store, run_id=latest.run_id))
        for message in message_history:
            if isinstance(message, ModelResponse):
                usage.requests += 1
                usage.incr(message.usage)
        # Count the complete recovered message chain instead of only the latest
        # attempt's events. This remains correct after more than one recovery.
        usage.tool_calls = _history_tool_call_count(message_history)

    return _RecoveryPlan(
        run_id=f"{conversation_id}-attempt-{len(runs) + 1}",
        parent_run_id=latest.run_id,
        message_history=message_history,
        usage=usage,
    )


class _TruncateOnlyToolOutputLimits(ToolOutputLimits[_Deps]):
    """Use Harness truncation without exposing its Spill read-back tool.

    Harness registers ``read_tool_result`` for configurations that may Spill.
    This runtime never Spills, so adding that fourth tool would violate the
    closed investigation capability set.
    """

    def get_toolset(self) -> None:
        return None


def _guard_tool_call(ctx: RunContext[_Deps], call: ToolCallInfo) -> GuardrailResult:
    if call.name not in {"list_metrics", "describe_metric", "query_metric"}:
        return GuardrailResult.block("TOOL_NOT_ALLOWED")
    metric_id = call.args.get("metric_id")
    if metric_id is not None and str(metric_id) not in ctx.deps.allowed_metric_ids:
        return GuardrailResult.block("METRIC_OUT_OF_SCOPE")
    if call.name == "query_metric":
        try:
            window = int(call.args.get("window_minutes", 0))
        except (TypeError, ValueError):
            return GuardrailResult.block("METRIC_WINDOW_INVALID")
        if window < 1 or window > 1_440:
            return GuardrailResult.block("METRIC_WINDOW_INVALID")
    return GuardrailResult.allow()


class PydanticInvestigatorRuntime(InvestigatorRuntime):
    def __init__(
        self,
        *,
        model_factory: SecureModelFactory | None = None,
        journal_path: Path | None = None,
        model_override: Model | None = None,
        step_store: StepStore | None = None,
    ) -> None:
        self._model_factory = model_factory or SecureModelFactory()
        self._journal_path = journal_path
        self._model_override = model_override
        self._step_store = step_store

    async def run(self, request: InvestigatorRequest, *, tools: ReadOnlyMetricTools) -> InvestigatorResult:
        store = self._step_store or (
            SqliteStepStore(database=self._journal_path, max_snapshots_per_run=3)
            if self._journal_path is not None
            else InMemoryStepStore(max_snapshots_per_run=3)
        )
        conversation_id = f"investigation-{request.snapshot.investigation_id}"
        tools.raise_if_canceled()
        # Decide whether it is safe to resume before constructing an outbound
        # client or performing any fresh model-visible tool work.
        recovery = await _recovery_plan(store, conversation_id=conversation_id)
        tools.raise_if_canceled()
        descriptors = await tools.list_metrics()
        allowed_metric_ids = {item.metric_id for item in descriptors}
        evidence_ids = {
            str(item.get("evidence_id"))
            for item in request.snapshot.alert_evidence
            if item.get("evidence_id")
        } | {item.evidence_id for item in request.snapshot.metric_evidence}

        evidence_ids.update(_history_evidence_ids(recovery.message_history))
        run_id = recovery.run_id
        capabilities: list[Any] = [
            StepPersistence(
                store=store,
                agent_name="incident-investigator",
                run_id=run_id,
                parent_run_id=recovery.parent_run_id,
                metadata={"investigation_id": request.snapshot.investigation_id},
            ),
            ToolGuardrail(guard=_guard_tool_call),
            _TruncateOnlyToolOutputLimits(
                bands=[Band(over=request.limits.tool_output_chars, action=Truncate(max_chars=request.limits.tool_output_chars))]
            ),
        ]

        async def list_metrics(ctx: RunContext[_Deps]) -> list[dict[str, object]]:
            values = await ctx.deps.tools.list_metrics()
            return [
                {"metric_id": item.metric_id, "display_name": item.display_name, "description": item.description, "unit": item.unit}
                for item in values
            ]

        async def describe_metric(ctx: RunContext[_Deps], metric_id: str) -> dict[str, object]:
            item = await ctx.deps.tools.describe_metric(metric_id)
            return {"metric_id": item.metric_id, "display_name": item.display_name, "description": item.description, "unit": item.unit}

        async def query_metric(ctx: RunContext[_Deps], metric_id: str, window_minutes: int) -> dict[str, object]:
            item = await ctx.deps.tools.query_metric(metric_id, window_minutes)
            evidence_ids.add(item.evidence_id)
            return {
                "evidence_id": item.evidence_id,
                "metric_id": item.metric_id,
                "status": item.status,
                "summary": dict(item.summary),
                "sample": list(item.sample),
            }

        connection_test_rule = (
            "这是连接契约测试：必须且只能调用一次 query_metric，metric_id=synthetic_metric，"
            "window_minutes=15，然后基于返回 evidence_id 生成结构化结论。"
            if request.connection_test
            else ""
        )
        secure: SecureModel | None = None
        try:
            if self._model_override is None:
                secure = self._model_factory.create(request.provider_profile, api_key=request.api_key)
                model = secure.model
            else:
                model = self._model_override
            model = _CancellationCheckedModel(
                model,
                tools.raise_if_canceled,
                request.usage_sink,
            )
            agent: Agent[_Deps, ReportOutput] = Agent(
                model,
                deps_type=_Deps,
                output_type=ReportOutput,
                instructions=(
                    "你是只读取证的 Incident 调查助手。只使用提供的三个指标工具；"
                    "不得声称执行了修复。所有人类可读字段必须使用简体中文，结论必须引用 evidence_ids。"
                    "当前工具只能佐证告警条件、趋势与相关性，不能确认人工语义上的事件、根因或因果关系；"
                    "告警生成表达式派生的主曲线与告警是同源证据，不能当作第二份独立佐证。"
                    + connection_test_rule
                ),
                tools=[list_metrics, describe_metric, query_metric],
                retries=0,
                tool_timeout=30.0,
                max_concurrency=1,
                capabilities=capabilities,
                model_settings=cast(ModelSettings, dict(request.provider_profile.settings or {})),
            )
            prompt = self._prompt(request)
            try:
                async with asyncio.timeout(request.limits.timeout_seconds):
                    result = await agent.run(
                        prompt if not recovery.message_history else None,
                        message_history=recovery.message_history or None,
                        conversation_id=conversation_id,
                        deps=_Deps(tools, allowed_metric_ids),
                        usage=recovery.usage,
                        usage_limits=UsageLimits(
                            request_limit=request.limits.request_limit,
                            tool_calls_limit=request.limits.tool_call_limit,
                            total_tokens_limit=request.limits.total_token_limit,
                            # Compatible providers do not all implement preflight token
                            # counting. UsageLimits still stops the next paid request
                            # after the previous response reports crossing the limit.
                            count_tokens_before_request=False,
                        ),
                    )
            except UnexpectedModelBehavior as exc:
                raise InvestigatorOutputContractError() from exc
            except InvestigationCanceled:
                raise
            except Exception as exc:  # noqa: BLE001 - reduce provider detail here
                raise InvestigatorRuntimeError(_probe_error_code(exc)) from exc
            report = self._report(
                result.output,
                degraded_domains=request.snapshot.degraded_domains,
            )
            try:
                validated = validate_report(
                    report,
                    available_evidence_ids=evidence_ids,
                    allowed_degraded_domains=set(request.snapshot.degraded_domains),
                )
            except ValueError as exc:
                if str(exc).startswith("REPORT_"):
                    raise InvestigatorOutputContractError() from exc
                raise
            usage = result.usage
            runs = sorted(
                await store.list_runs(conversation_id=conversation_id),
                key=lambda item: (item.started_at, item.run_id),
            )
            events = [
                event
                for item in runs
                for event in await store.list_events(run_id=item.run_id)
            ]
            activities = tuple(
                InvestigationActivityV2(index + 1, event.kind, "RECORDED", "OK")
                for index, event in enumerate(events)
            )
            return InvestigatorResult(
                report=validated,
                activities=activities,
                request_count=usage.requests,
                tool_call_count=usage.tool_calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                run_id=run_id,
            )
        finally:
            if secure is not None:
                await secure.http_client.aclose()

    @staticmethod
    def _prompt(request: InvestigatorRequest) -> str:
        alerts = [dict(item) for item in request.snapshot.alert_evidence]
        metrics = [
            {
                "evidence_id": item.evidence_id,
                "metric_id": item.metric_id,
                "status": item.status,
                "summary": dict(item.summary),
                "sample": list(item.sample),
            }
            for item in request.snapshot.metric_evidence
        ]
        coverage = {
            "total": len(request.snapshot.member_alert_refs)
            or len(request.snapshot.alert_evidence),
            "detailed": len(request.snapshot.alert_evidence),
            "summarized": list(request.snapshot.alert_coverage),
        }
        # Full member refs remain in the durable snapshot. Only bounded details and
        # aggregate coverage cross the model boundary; L3/raw payloads never do.
        return (
            f"调查冻结告警与已有指标证据。告警证据={alerts!r}"
            f"\n成员覆盖={coverage!r}\n指标证据={metrics!r}"
        )

    @staticmethod
    def _report(
        value: ReportOutput,
        *,
        degraded_domains: tuple[str, ...],
    ) -> InvestigationReportV2:
        return InvestigationReportV2(
            summary_zh=value.summary_zh,
            verdict=InvestigationVerdict(value.verdict),
            confidence=value.confidence,
            findings=tuple(EvidenceFindingV2(item.title_zh, item.analysis_zh, tuple(item.evidence_ids)) for item in value.findings),
            recommended_actions=tuple(InvestigationActionV2(item.title_zh, item.rationale_zh, tuple(item.evidence_ids)) for item in value.recommended_actions),
            missing_evidence_zh=tuple(value.missing_evidence_zh),
            # Degradation is a frozen repository fact, not a provider opinion.
            degraded_domains=degraded_domains,
            evidence_gain=value.evidence_gain,
        )


class _SyntheticMetricTools(ReadOnlyMetricTools):
    def raise_if_canceled(self) -> None:
        return None

    async def list_metrics(self) -> tuple[MetricDescriptorV2, ...]:
        return (
            MetricDescriptorV2(
                "synthetic_metric",
                "合成测试指标",
                "只用于验证模型工具调用与结构化输出契约",
                "ratio",
            ),
        )

    async def describe_metric(self, metric_id: str) -> MetricDescriptorV2:
        if metric_id != "synthetic_metric":
            raise LookupError("METRIC_NOT_FOUND")
        return (await self.list_metrics())[0]

    async def query_metric(
        self, metric_id: str, window_minutes: int
    ) -> MetricObservationV2:
        if metric_id != "synthetic_metric" or window_minutes != 15:
            raise ValueError("METRIC_TEST_ARGUMENT_INVALID")
        return MetricObservationV2(
            "probe-metric-1",
            metric_id,
            "DATA",
            {"latest": 1.0, "minimum": 0.5, "maximum": 1.0},
            ((1.0, "0.5"), (2.0, "1.0")),
        )


def _probe_error_code(exc: Exception) -> str:
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        if getattr(current, "code", None) == "MODEL_OUTPUT_CONTRACT_INVALID":
            return "MODEL_OUTPUT_CONTRACT_INVALID"
        message = str(current)
        for code in (
            "MODEL_EGRESS_HTTPS_REQUIRED",
            "MODEL_EGRESS_PORT_REJECTED",
            "MODEL_EGRESS_DNS_EMPTY",
            "MODEL_EGRESS_PRIVATE_ADDRESS",
        ):
            if code in message:
                return code
        name = type(current).__name__
        if name in {"AuthenticationError", "PermissionDeniedError"}:
            return "MODEL_AUTH_FAILED"
        if name == "RateLimitError":
            return "MODEL_RATE_LIMITED"
        if name in {"TimeoutError", "APITimeoutError"} or isinstance(
            current, httpx.TimeoutException
        ):
            return "MODEL_TIMEOUT"
        if name in {"UsageLimitExceeded", "RequestUsageLimitExceeded"}:
            return "MODEL_BUDGET_EXCEEDED"
        current = current.__cause__ or current.__context__
    if isinstance(exc, ValueError) and str(exc).startswith("REPORT_"):
        return "MODEL_OUTPUT_CONTRACT_INVALID"
    return "MODEL_SERVICE_UNAVAILABLE"


class PydanticInvestigatorProbe:
    """Run the exact production Investigator against a synthetic evidence snapshot."""

    def __init__(
        self,
        *,
        runtime: InvestigatorRuntime | None = None,
        model_factory: SecureModelFactory | None = None,
        directory: OpenAICompatibleProbe | None = None,
    ) -> None:
        self._runtime = runtime or PydanticInvestigatorRuntime(model_factory=model_factory)
        self._directory = directory or OpenAICompatibleProbe()

    async def test(
        self,
        *,
        profile: ProviderProfile,
        api_key: str,
    ) -> ModelProbeResult:
        if not api_key.strip() or not profile.model_id.strip():
            return ModelProbeResult(False, "MODEL_CONFIGURATION_INCOMPLETE")
        request = InvestigatorRequest(
            snapshot=EvidenceSnapshotV2(
                investigation_id="connection-test",
                occurrence_id=0,
                alert_evidence=(
                    {
                        "evidence_id": "probe-alert-1",
                        "alertname": "SyntheticModelContract",
                        "summary": "仅用于连接契约测试，不包含真实告警数据。",
                    },
                ),
                metric_evidence=(),
                degraded_domains=(),
            ),
            provider_profile=profile,
            api_key=api_key,
            limits=InvestigationRunLimits(
                request_limit=2,
                tool_call_limit=1,
                total_token_limit=8_000,
                timeout_seconds=60,
                tool_output_chars=2_000,
            ),
            connection_test=True,
        )
        try:
            result = await self._runtime.run(request, tools=_SyntheticMetricTools())
        except Exception as exc:  # noqa: BLE001 - reduced to a safe diagnostic code
            return ModelProbeResult(False, _probe_error_code(exc))
        if result.tool_call_count != 1 or result.report.evidence_gain < 1:
            return ModelProbeResult(False, "MODEL_INVESTIGATOR_CONTRACT_INVALID")
        return ModelProbeResult(True, "OK")

    async def list_models(
        self,
        *,
        base_url: str,
        api_key: str,
    ) -> tuple[str, ...]:
        return await self._directory.list_models(base_url=base_url, api_key=api_key)


def offline_investigator_runtime(*, journal_path: Path | None = None) -> InvestigatorRuntime:
    """Deterministic FunctionModel for the explicit ``--mock`` acceptance mode."""

    def respond(messages: list[Any], agent_info: Any) -> ModelResponse:
        if "ToolReturnPart" not in repr(messages):
            return ModelResponse(
                parts=[ToolCallPart("list_metrics", {}, "offline-list")]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    agent_info.output_tools[0].name,
                    {
                        "summary_zh": "离线调查未取得新增指标证据，结果仅用于本地验收。",
                        "verdict": "INCONCLUSIVE",
                        "confidence": 0.5,
                        "findings": [],
                        "recommended_actions": [],
                        "missing_evidence_zh": ["离线模式不代表真实模型服务结果。"],
                        "evidence_gain": 0,
                    },
                    "offline-output",
                )
            ]
        )

    return PydanticInvestigatorRuntime(
        model_override=FunctionModel(respond),
        journal_path=journal_path,
    )


__all__ = [
    "InvestigatorOutputContractError",
    "InvestigatorRecoveryError",
    "InvestigatorRuntimeError",
    "ProviderProfileRegistry",
    "PydanticInvestigatorRuntime",
    "PydanticInvestigatorProbe",
    "SecureModelFactory",
    "offline_investigator_runtime",
]
