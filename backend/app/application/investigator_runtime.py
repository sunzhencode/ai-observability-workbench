"""Repository-owned port for running a bounded incident investigation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from app.domains.investigations.runtime import (
    EvidenceSnapshotV2,
    InvestigationActivityV2,
    InvestigationReportV2,
    MetricDescriptorV2,
    MetricObservationV2,
    ProviderProfile,
)


class ReadOnlyMetricTools(Protocol):
    def raise_if_canceled(self) -> None: ...

    async def list_metrics(self) -> tuple[MetricDescriptorV2, ...]: ...

    async def describe_metric(self, metric_id: str) -> MetricDescriptorV2: ...

    async def query_metric(self, metric_id: str, window_minutes: int) -> MetricObservationV2: ...


@dataclass(frozen=True, slots=True)
class InvestigationRunLimits:
    request_limit: int = 6
    tool_call_limit: int = 10
    total_token_limit: int = 60_000
    timeout_seconds: int = 180
    tool_output_chars: int = 8_000


@dataclass(frozen=True, slots=True)
class InvestigatorUsageDelta:
    request_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class InvestigatorRequest:
    snapshot: EvidenceSnapshotV2
    provider_profile: ProviderProfile
    api_key: str
    limits: InvestigationRunLimits = InvestigationRunLimits()
    connection_test: bool = False
    usage_sink: Callable[[InvestigatorUsageDelta], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class InvestigatorResult:
    report: InvestigationReportV2
    activities: tuple[InvestigationActivityV2, ...]
    request_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    run_id: str


class InvestigatorRuntime(Protocol):
    async def run(
        self,
        request: InvestigatorRequest,
        *,
        tools: ReadOnlyMetricTools,
    ) -> InvestigatorResult: ...


__all__ = [
    "InvestigationRunLimits",
    "InvestigatorUsageDelta",
    "InvestigatorRequest",
    "InvestigatorResult",
    "InvestigatorRuntime",
    "ReadOnlyMetricTools",
]
