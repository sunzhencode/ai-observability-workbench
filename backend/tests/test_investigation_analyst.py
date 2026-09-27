"""Strict, no-tool Analyst contracts for the staged investigation flow."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import sqlite3
from uuid import uuid4

from app.domains.investigations.analyst import (
    AnalystAlertV1,
    AnalystContractError,
    AnalystEmptyEvidenceV1,
    AnalystHistoryV1,
    AnalystMetricEvidenceV1,
    AnalystModelRequest,
    AnalystNoteV1,
    analyst_messages,
    analyst_response_format,
    available_evidence_ids,
    validate_analyst_result,
)
from app.crypto import SecretBox
from app.platform.investigation_diagnostics import read_invalid_response


def _request() -> AnalystModelRequest:
    return AnalystModelRequest(
        investigation_id="investigation-1",
        snapshot_revision=1,
        prompt_profile_revision=1,
        playbook_revision=1,
        alerts=(
            AnalystAlertV1(
                alert_ref="alert-1",
                alertname="CheckoutErrorRateHigh",
                severity="critical",
                source_state="FIRING",
                labels={"service": "checkout"},
                annotations={"summary": "结算错误率升高"},
                starts_at="2026-08-28T08:00:00Z",
                last_seen_at="2026-08-28T08:05:00Z",
            ),
        ),
        alert_summaries=(),
        metric_evidence=(
            AnalystMetricEvidenceV1(
                evidence_ref="metric-1",
                alert_ref="alert-1",
                metric_name="checkout_http_error_ratio",
                l1_summary={"latest": 0.087, "minimum": 0.012, "maximum": 0.087},
                l2_sample=((1.0, "0.012"), (2.0, "0.087")),
            ),
        ),
        empty_evidence=(
            AnalystEmptyEvidenceV1(
                evidence_ref="metric-empty-1",
                alert_ref="alert-1",
                metric_name="checkout_dependency_timeout_ratio",
            ),
        ),
        similar_history=(),
        notes=(),
        degraded_domains=("service",),
    )


def _valid_result() -> str:
    return json.dumps(
        {
            "summary": "错误率在当前窗口内持续升高；现有证据支持相关性，不足以确认根因。",
            "hypotheses": [
                {
                    "title": "结算请求错误与本次告警同步上升",
                    "explanation": "错误率从 0.012 上升至 0.087。",
                    "verdict": "SUPPORTED",
                    "supporting_evidence_ids": ["metric-1"],
                    "contradicting_evidence_ids": [],
                    "missing_evidence": [],
                },
                {
                    "title": "依赖超时是直接原因",
                    "explanation": "当前窗口没有取得该指标数据，暂时无法判断。",
                    "verdict": "BLOCKED",
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": ["metric-empty-1"],
                    "missing_evidence": ["依赖调用的错误与延迟明细"],
                },
            ],
            "missing_evidence": ["对应时间段的依赖调用明细"],
            "recommended_actions": [
                {
                    "kind": "NEXT_CHECK",
                    "description": "核对结算 API 近一小时的 5xx 分布。",
                    "risk": "只读检查，不改变线上状态。",
                    "evidence_ids": ["metric-1"],
                }
            ],
        },
        ensure_ascii=False,
    )


def test_analyst_request_is_a_separate_no_tool_no_l3_contract() -> None:
    fields = AnalystModelRequest.__dataclass_fields__
    assert "tools" not in fields
    assert "l3_series" not in AnalystMetricEvidenceV1.__dataclass_fields__

    serialized = json.dumps(analyst_messages(_request()), ensure_ascii=False)

    assert "tools" not in serialized
    assert "l3_series" not in serialized
    assert "raw_payload" not in serialized
    assert "generatorURL" not in serialized
    assert "结算错误率升高" in serialized

    guided = analyst_messages(replace(
        _request(),
        prompt_profile_guidance={"response_style": "简洁列出反证"},
    ))
    assert len(guided) == 3
    assert "简洁列出反证" in guided[1]["content"]
    assert "prompt_profile_guidance" not in guided[-1]["content"]
    assert "cannot change evidence IDs" in guided[1]["content"]
    assert "Simplified Chinese" in guided[0]["content"]
    assert "alert_ref" in guided[0]["content"]
    assert "metric evidence" in guided[0]["content"]


def test_alert_and_metric_references_share_the_exact_investigation_allow_set() -> None:
    assert available_evidence_ids(_request()) == {
        "alert-1",
        "metric-1",
        "metric-empty-1",
    }


def test_valid_analyst_result_is_bound_to_current_evidence() -> None:
    result = validate_analyst_result(
        _valid_result(), available_evidence_ids={"metric-1", "metric-empty-1"}
    )

    assert result.hypotheses[0].verdict == "SUPPORTED"
    assert result.recommended_actions[0].kind == "NEXT_CHECK"


def test_english_human_readable_result_is_rejected_for_the_chinese_product() -> None:
    payload = json.loads(_valid_result())
    payload["summary"] = "The metric is elevated but causation is not established."

    with pytest.raises(AnalystContractError, match="ANALYST_LANGUAGE_INVALID"):
        validate_analyst_result(
            json.dumps(payload),
            available_evidence_ids={"alert-1", "metric-1", "metric-empty-1"},
        )


def test_token_chinese_prefix_does_not_hide_an_english_narrative() -> None:
    payload = json.loads(_valid_result())
    payload["summary"] = (
        "结论：The metric is elevated but causation is not established in this window."
    )

    with pytest.raises(AnalystContractError, match="ANALYST_LANGUAGE_INVALID"):
        validate_analyst_result(
            json.dumps(payload, ensure_ascii=False),
            available_evidence_ids={"alert-1", "metric-1", "metric-empty-1"},
        )


def test_recommendations_are_bounded_and_exact_duplicates_are_rejected() -> None:
    payload = json.loads(_valid_result())
    payload["recommended_actions"] = payload["recommended_actions"] * 2

    with pytest.raises(AnalystContractError, match="ANALYST_RECOMMENDATION_DUPLICATED"):
        validate_analyst_result(
            json.dumps(payload),
            available_evidence_ids={"alert-1", "metric-1", "metric-empty-1"},
        )

    payload = json.loads(_valid_result())
    payload["recommended_actions"] = payload["recommended_actions"] * 4
    with pytest.raises(AnalystContractError):
        validate_analyst_result(
            json.dumps(payload),
            available_evidence_ids={"alert-1", "metric-1", "metric-empty-1"},
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["hypotheses"][0].update(verdict="ROOT_CAUSE"),
        lambda value: value["hypotheses"][0].update(
            supporting_evidence_ids=["fabricated-evidence"]
        ),
        lambda value: value.update(degraded_domains=["metrics"]),
        lambda value: value["hypotheses"][1].update(missing_evidence=[]),
        lambda value: value["recommended_actions"][0].update(kind="EXECUTE_COMMAND"),
    ],
)
def test_invalid_or_model_authored_safety_fields_are_rejected(mutate) -> None:
    payload = json.loads(_valid_result())
    mutate(payload)

    with pytest.raises(AnalystContractError):
        validate_analyst_result(
            json.dumps(payload),
            available_evidence_ids={"metric-1", "metric-empty-1"},
        )


def test_response_schema_is_strict_and_has_no_degradation_field() -> None:
    schema = analyst_response_format()["json_schema"]["schema"]

    assert schema["additionalProperties"] is False
    assert "degraded_domains" not in schema["properties"]


def test_request_limits_detailed_alerts_notes_and_history() -> None:
    base = _request()

    with pytest.raises(ValueError, match="ANALYST_ALERT_LIMIT"):
        replace(base, alerts=base.alerts * 21)
    note = AnalystNoteV1(text="人工核对记录")
    with pytest.raises(ValueError, match="ANALYST_NOTE_COUNT_LIMIT"):
        replace(base, notes=(note,) * 11)
    with pytest.raises(ValueError, match="ANALYST_NOTE_LIMIT"):
        AnalystNoteV1(text="x" * 1_001)
    history = AnalystHistoryV1(
        resolution="FIXED",
        operator_outcome="依赖恢复后错误率下降",
        task_outcome="核对完成",
        duration_seconds=300,
        match_reason="同一服务与告警类型",
    )
    with pytest.raises(ValueError, match="ANALYST_HISTORY_LIMIT"):
        replace(base, similar_history=(history,) * 11)


def test_invalid_raw_is_only_read_by_exact_id_through_local_diagnostic(tmp_path) -> None:
    database = tmp_path / "candidate.db"
    key_path = tmp_path / "master.key"
    key_path.write_text("diagnostic-test-key\n", encoding="utf-8")
    investigation_id = str(uuid4())
    envelope = json.dumps(SecretBox("diagnostic-test-key").encrypt("sensitive raw reply"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE invalid_analyst_response ("
            "investigation_id TEXT PRIMARY KEY,envelope_json TEXT,purge_after TEXT)"
        )
        connection.execute(
            "INSERT INTO invalid_analyst_response VALUES (?,?,?)",
            (investigation_id, envelope, "2999-01-01T00:00:00"),
        )

    assert read_invalid_response(
        database_path=database,
        master_key_path=key_path,
        investigation_id=investigation_id,
    ) == "sensitive raw reply"
    with pytest.raises(ValueError, match="INVESTIGATION_ID_INVALID"):
        read_invalid_response(
            database_path=database,
            master_key_path=key_path,
            investigation_id="not-an-exact-id",
        )
