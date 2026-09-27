"""Operator-facing guidance for typed investigation degradations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DegradationGuidanceV1:
    impact: str
    preserved: str
    next_step: str


def degradation_display_domain(recorded_domain: str, code: str) -> str:
    """Correct known legacy classification errors without rewriting audit rows."""

    return "model" if code == "MODEL_RATE_LIMITED" else recorded_domain


def degradation_display_message(
    recorded_domain: str, code: str, recorded_message: str
) -> str:
    """Project canonical operator copy while preserving the recorded audit value."""

    del recorded_domain
    if code == "MODEL_RATE_LIMITED":
        return "模型服务限流或用量受限；已有证据已保留，平台未自动重试"
    return recorded_message


def degradation_guidance(domain: str, code: str) -> DegradationGuidanceV1:
    """Describe a safe code without exposing an upstream address or response."""

    if code == "SOURCE_UNAVAILABLE":
        return DegradationGuidanceV1(
            "本次没有取得该指标源的新证据。",
            "告警范围、已经完成的指标读取和人工处置事实仍保留。",
            "在设置中核对该来源的指标连接；恢复后显式重新调查。",
        )
    if code == "ZERO_HOP_DEADLINE_REACHED":
        return DegradationGuidanceV1(
            "超出 20 秒的指标读取没有进入本次基础证据。",
            "截止时间前已经取得的事实仍保留。",
            "检查指标源响应时间；需要新结果时显式重新调查。",
        )
    if code == "METRIC_READ_PLAN_EMPTY":
        return DegradationGuidanceV1(
            "本次没有可安全执行的指标读取计划。",
            "来源、告警成员和告警正文范围仍保留。",
            "为该告警补充可用主曲线或指标模板后重新调查。",
        )
    if code == "SERVICE_UNMAPPED":
        return DegradationGuidanceV1(
            "本次不能按服务收敛证据或匹配相似历史。",
            "来源与全部告警成员仍保留。",
            "为事件选择有效服务；需要服务范围证据时重新调查。",
        )
    if code == "HISTORY_NOT_AVAILABLE":
        return DegradationGuidanceV1(
            "本次结论没有相似历史处置作为参照。",
            "当前告警、指标、人工 Note 与任务事实仍保留。",
            "继续按当前证据人工判断；历史检索接入后再发起新调查。",
        )
    if code == "CLAIM_LEASE_EXPIRED":
        return DegradationGuidanceV1(
            "上一次证据准备没有完成。",
            "上一次调查记录和已经写入的事实仍保留。",
            "直接重新发起调查；平台会使用新的准备租约。",
        )
    if code == "MODEL_CHANNEL_UNAVAILABLE":
        return DegradationGuidanceV1(
            "没有继续生成 AI 扩展证据或结构化结论。",
            "基础证据和人工处置事实仍保留。",
            "在设置中核对已启用模型服务；恢复后显式重新调查。",
        )
    if code == "MODEL_RATE_LIMITED":
        return DegradationGuidanceV1(
            "模型服务拒绝了本次调用，本次没有生成可采用的新结果。",
            "基础证据、已完成的扩展读取和人工处置事实仍保留；平台没有自动重试。",
            "稍后显式重新调查；若持续发生，请在模型服务控制台检查用量与限额。",
        )
    if domain == "model" or code.startswith(("MODEL_", "PLANNER_", "ANALYST_")):
        return DegradationGuidanceV1(
            "模型阶段没有产出可采用的新结果。",
            "基础证据、已完成的扩展读取和人工处置事实仍保留。",
            "按 safe code 核对模型能力或服务状态；需要新结果时显式重新调查。",
        )
    if domain == "budget" or "BUDGET" in code or "LIMIT" in code:
        return DegradationGuidanceV1(
            "调查在代码预算边界处停止，没有继续外部调用。",
            "到达边界前已经取得的全部事实仍保留。",
            "先按现有证据处理；只有目标或上游事实变化时再发起新调查。",
        )
    return DegradationGuidanceV1(
        "该证据域未完整交付。",
        "页面继续保留已经成功读取的事实和人工处置状态。",
        "记录此 safe code，核对对应配置或服务状态后再决定是否重新调查。",
    )


__all__ = [
    "DegradationGuidanceV1",
    "degradation_display_domain",
    "degradation_display_message",
    "degradation_guidance",
]
