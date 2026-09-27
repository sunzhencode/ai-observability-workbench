"""The investigation fault matrix must always say impact, preservation and next step."""

from app.domains.investigations.degradation import (
    degradation_display_domain,
    degradation_display_message,
    degradation_guidance,
)


def test_runtime_degradation_codes_have_distinct_actionable_guidance() -> None:
    cases = {
        ("metrics", "SOURCE_UNAVAILABLE"),
        ("metrics", "ZERO_HOP_DEADLINE_REACHED"),
        ("metrics", "METRIC_READ_PLAN_EMPTY"),
        ("service", "SERVICE_UNMAPPED"),
        ("history", "HISTORY_NOT_AVAILABLE"),
        ("claim", "CLAIM_LEASE_EXPIRED"),
        ("model", "MODEL_CHANNEL_UNAVAILABLE"),
        ("model", "MODEL_TIMEOUT"),
        ("model", "PLANNER_RESPONSE_INVALID"),
        ("model", "ANALYST_CONTRACT_REJECTED"),
        ("budget", "PLANNER_ROUND_LIMIT"),
    }

    rendered = {
        key: degradation_guidance(*key)
        for key in cases
    }

    assert all(value.impact for value in rendered.values())
    assert all("保留" in value.preserved for value in rendered.values())
    assert all(value.next_step for value in rendered.values())
    assert rendered[("metrics", "SOURCE_UNAVAILABLE")].impact != rendered[
        ("history", "HISTORY_NOT_AVAILABLE")
    ].impact


def test_unknown_safe_code_fails_readably_without_guessing_root_cause() -> None:
    guidance = degradation_guidance("future-domain", "FUTURE_SAFE_CODE")

    assert "未完整交付" in guidance.impact
    assert "safe code" in guidance.next_step
    assert "根因" not in " ".join(
        (guidance.impact, guidance.preserved, guidance.next_step)
    )


def test_legacy_rate_limit_record_projects_the_canonical_model_message() -> None:
    assert degradation_display_domain("budget", "MODEL_RATE_LIMITED") == "model"
    assert degradation_display_message(
        "budget",
        "MODEL_RATE_LIMITED",
        "调查已到达代码预算边界，未继续发起外部调用",
    ) == "模型服务限流或用量受限；已有证据已保留，平台未自动重试"
