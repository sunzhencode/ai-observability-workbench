"""Framework-independent contracts for the unified Incident Investigator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Mapping


class ProviderId(StrEnum):
    OPENAI = "OPENAI"
    DEEPSEEK = "DEEPSEEK"
    MOONSHOT = "MOONSHOT"
    ZHIPU = "ZHIPU"
    DASHSCOPE = "DASHSCOPE"
    CUSTOM = "CUSTOM"


class ProtocolProfile(StrEnum):
    RESPONSES = "RESPONSES"
    CHAT_COMPLETIONS = "CHAT_COMPLETIONS"


class ProviderSupportLevel(StrEnum):
    REVIEWED = "REVIEWED"
    COMPATIBLE = "COMPATIBLE"
    BEST_EFFORT = "BEST_EFFORT"


class InvestigationRunState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class InvestigationVerdict(StrEnum):
    INCIDENT_CONFIRMED = "INCIDENT_CONFIRMED"
    LIKELY_INCIDENT = "LIKELY_INCIDENT"
    INCONCLUSIVE = "INCONCLUSIVE"
    NO_INCIDENT_EVIDENCE = "NO_INCIDENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    provider_id: ProviderId
    protocol: ProtocolProfile
    support_level: ProviderSupportLevel
    base_url: str
    model_id: str = ""
    settings: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class EvidenceFindingV2:
    title_zh: str
    analysis_zh: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvestigationActionV2:
    title_zh: str
    rationale_zh: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvestigationReportV2:
    summary_zh: str
    verdict: InvestigationVerdict
    confidence: float
    findings: tuple[EvidenceFindingV2, ...]
    recommended_actions: tuple[InvestigationActionV2, ...]
    missing_evidence_zh: tuple[str, ...]
    degraded_domains: tuple[str, ...]
    evidence_gain: int


@dataclass(frozen=True, slots=True)
class MetricDescriptorV2:
    metric_id: str
    display_name: str
    description: str
    unit: str


@dataclass(frozen=True, slots=True)
class MetricObservationV2:
    evidence_id: str
    metric_id: str
    status: str
    summary: Mapping[str, float | int | str | None]
    sample: tuple[tuple[float, str], ...]


@dataclass(frozen=True, slots=True)
class EvidenceSnapshotV2:
    investigation_id: str
    occurrence_id: int
    alert_evidence: tuple[Mapping[str, object], ...]
    metric_evidence: tuple[MetricObservationV2, ...]
    degraded_domains: tuple[str, ...]
    member_alert_refs: tuple[str, ...] = ()
    alert_coverage: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class InvestigationActivityV2:
    sequence: int
    kind: str
    status: str
    safe_code: str
    evidence_ids: tuple[str, ...] = ()


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CAUSAL_ASSERTION = re.compile(
    r"直接因果关系|根本原因|根因|原因(?:是|为|在于)|"
    r"(?:导致|造成|引发|致使|源于|归因于?)"
)
_CAUSAL_UNCERTAINTY = re.compile(
    r"可能|疑似|假设|推测|线索|尚未|尚不能|不能|无法|"
    r"未能|未确认|未证实|不确定|不足以|不排除|不能排除|"
    r"待(?:人工)?核对|需(?:要)?(?:人工)?核对|是否|相关(?:性)?|"
    r"缺少|缺乏|未提供|需要补充"
)
_CLAUSE_SEPARATOR = re.compile(r"[。！？；;!?\n]+")


def _require_chinese(value: str) -> None:
    if not value.strip() or _CJK.search(value) is None:
        raise ValueError("REPORT_CHINESE_REQUIRED")


def _reject_unsupported_causality(value: str) -> None:
    for clause in _CLAUSE_SEPARATOR.split(value):
        if _CAUSAL_ASSERTION.search(clause) and not _CAUSAL_UNCERTAINTY.search(clause):
            raise ValueError("REPORT_CAUSALITY_UNSUPPORTED")


def validate_report(
    report: InvestigationReportV2,
    *,
    available_evidence_ids: set[str],
    allowed_degraded_domains: set[str],
) -> InvestigationReportV2:
    """Validate provider output again at the repository trust boundary."""

    _require_chinese(report.summary_zh)
    _reject_unsupported_causality(report.summary_zh)
    if report.verdict is InvestigationVerdict.INCIDENT_CONFIRMED:
        raise ValueError("REPORT_INCIDENT_CONFIRMATION_RESERVED")
    if not 0.0 <= report.confidence <= 1.0:
        raise ValueError("REPORT_CONFIDENCE_INVALID")
    if report.evidence_gain < 0:
        raise ValueError("REPORT_EVIDENCE_GAIN_INVALID")
    if len(report.recommended_actions) > 3:
        raise ValueError("REPORT_ACTION_LIMIT")
    if len({item.title_zh.strip() for item in report.recommended_actions}) != len(
        report.recommended_actions
    ):
        raise ValueError("REPORT_ACTION_DUPLICATE")

    referenced: set[str] = set()
    for finding in report.findings:
        _require_chinese(finding.title_zh)
        _require_chinese(finding.analysis_zh)
        _reject_unsupported_causality(finding.title_zh)
        _reject_unsupported_causality(finding.analysis_zh)
        if not finding.evidence_ids:
            raise ValueError("REPORT_FINDING_EVIDENCE_REQUIRED")
        referenced.update(finding.evidence_ids)
    for action in report.recommended_actions:
        _require_chinese(action.title_zh)
        _require_chinese(action.rationale_zh)
        _reject_unsupported_causality(action.title_zh)
        _reject_unsupported_causality(action.rationale_zh)
        if not action.evidence_ids:
            raise ValueError("REPORT_ACTION_EVIDENCE_REQUIRED")
        referenced.update(action.evidence_ids)
    for missing in report.missing_evidence_zh:
        _require_chinese(missing)
        _reject_unsupported_causality(missing)
    if not referenced.issubset(available_evidence_ids):
        raise ValueError("REPORT_EVIDENCE_UNKNOWN")
    if not set(report.degraded_domains).issubset(allowed_degraded_domains):
        raise ValueError("REPORT_DEGRADED_DOMAIN_UNKNOWN")
    if report.evidence_gain == 0 and "未取得" not in report.summary_zh:
        raise ValueError("REPORT_ZERO_GAIN_NOT_DISCLOSED")
    return report


__all__ = [
    "EvidenceFindingV2",
    "EvidenceSnapshotV2",
    "InvestigationActionV2",
    "InvestigationActivityV2",
    "InvestigationReportV2",
    "InvestigationRunState",
    "InvestigationVerdict",
    "MetricDescriptorV2",
    "MetricObservationV2",
    "ProtocolProfile",
    "ProviderId",
    "ProviderProfile",
    "ProviderSupportLevel",
    "validate_report",
]
