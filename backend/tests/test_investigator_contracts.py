from __future__ import annotations

import pytest

from app.domains.investigations.runtime import (
    EvidenceFindingV2,
    InvestigationActionV2,
    InvestigationReportV2,
    InvestigationVerdict,
    validate_report,
)


def _report(**changes: object) -> InvestigationReportV2:
    values: dict[str, object] = {
        "summary_zh": "错误率在调查窗口内持续高于本地演示值，但现有证据不足以确认根因。",
        "verdict": InvestigationVerdict.LIKELY_INCIDENT,
        "confidence": 0.72,
        "findings": (
            EvidenceFindingV2(
                title_zh="错误率持续偏高",
                analysis_zh="指标在整个窗口保持非零，支持告警条件仍然存在。",
                evidence_ids=("metric-1",),
            ),
        ),
        "recommended_actions": (
            InvestigationActionV2(
                title_zh="核对状态码分布",
                rationale_zh="先确认错误是否集中在少数接口，再决定后续人工处置。",
                evidence_ids=("metric-1",),
            ),
        ),
        "missing_evidence_zh": ("缺少同窗口的接口级状态码分布。",),
        "degraded_domains": (),
        "evidence_gain": 1,
    }
    values.update(changes)
    return InvestigationReportV2(**values)  # type: ignore[arg-type]


def test_report_accepts_owned_evidence_and_chinese_human_text() -> None:
    report = validate_report(
        _report(),
        available_evidence_ids={"alert-1", "metric-1"},
        allowed_degraded_domains={"METRICS"},
    )
    assert report.verdict is InvestigationVerdict.LIKELY_INCIDENT


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"summary_zh": "error ratio is high"}, "REPORT_CHINESE_REQUIRED"),
        (
            {
                "findings": (
                    EvidenceFindingV2("未知证据", "引用了不属于本次调查的证据。", ("metric-x",)),
                )
            },
            "REPORT_EVIDENCE_UNKNOWN",
        ),
        (
            {
                "findings": (
                    EvidenceFindingV2("错误率持续偏高", "指标仍然高于演示值。", ()),
                )
            },
            "REPORT_FINDING_EVIDENCE_REQUIRED",
        ),
        (
            {
                "recommended_actions": (
                    InvestigationActionV2("核对状态码分布", "人工核对接口错误分布。", ()),
                )
            },
            "REPORT_ACTION_EVIDENCE_REQUIRED",
        ),
        ({"confidence": 1.1}, "REPORT_CONFIDENCE_INVALID"),
        (
            {"verdict": InvestigationVerdict.INCIDENT_CONFIRMED},
            "REPORT_INCIDENT_CONFIRMATION_RESERVED",
        ),
        (
            {
                "findings": (
                    EvidenceFindingV2(
                        "指标与告警存在直接因果关系",
                        "这进一步验证了指标与告警之间的直接因果关系。",
                        ("metric-1",),
                    ),
                )
            },
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        (
            {"summary_zh": "本次故障的原因是数据库连接池耗尽。"},
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        (
            {
                "recommended_actions": (
                    InvestigationActionV2(
                        "修复造成故障的发布",
                        "先由人工核对发布记录。",
                        ("metric-1",),
                    ),
                )
            },
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        (
            {"missing_evidence_zh": ("发布变更导致本次故障。",)},
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        (
            {
                "findings": (
                    EvidenceFindingV2(
                        "数据库异常",
                        "数据库连接池耗尽是本次故障的根因。",
                        ("metric-1",),
                    ),
                )
            },
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        (
            {
                "findings": (
                    EvidenceFindingV2(
                        "依赖服务超时",
                        "依赖服务超时导致错误率升高。",
                        ("metric-1",),
                    ),
                )
            },
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        (
            {
                "recommended_actions": (
                    InvestigationActionV2(
                        "检查发布变更",
                        "发布变更造成接口错误率上升。",
                        ("metric-1",),
                    ),
                )
            },
            "REPORT_CAUSALITY_UNSUPPORTED",
        ),
        ({"degraded_domains": ("LOGS",)}, "REPORT_DEGRADED_DOMAIN_UNKNOWN"),
        (
            {
                "recommended_actions": tuple(
                    InvestigationActionV2(f"建议{i}", "人工核对已有证据。", ("metric-1",))
                    for i in range(4)
                )
            },
            "REPORT_ACTION_LIMIT",
        ),
    ],
)
def test_report_rejects_contract_violations(changes: dict[str, object], code: str) -> None:
    with pytest.raises(ValueError, match=code):
        validate_report(
            _report(**changes),
            available_evidence_ids={"alert-1", "metric-1"},
            allowed_degraded_domains={"METRICS"},
        )


def test_zero_evidence_gain_is_explicit_in_summary() -> None:
    with pytest.raises(ValueError, match="REPORT_ZERO_GAIN_NOT_DISCLOSED"):
        validate_report(
            _report(evidence_gain=0),
            available_evidence_ids={"metric-1"},
            allowed_degraded_domains=set(),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "现有证据不足以确认数据库连接池是本次故障的根因。",
        "依赖服务超时可能导致错误率升高，仍需人工核对。",
        "尚不能确定发布变更是否造成接口错误率上升。",
        "缺少用于确认根因的调用链证据。",
    ],
)
def test_report_allows_explicitly_uncertain_causal_language(statement: str) -> None:
    report = validate_report(
        _report(
            findings=(
                EvidenceFindingV2("待核对假设", statement, ("metric-1",)),
            )
        ),
        available_evidence_ids={"metric-1"},
        allowed_degraded_domains=set(),
    )

    assert report.findings[0].analysis_zh == statement
