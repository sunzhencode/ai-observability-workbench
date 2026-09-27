from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from app.adapters.persistence.unified_investigations import (
    SqlAlchemyUnifiedInvestigationStore,
)
from app.application.unified_investigations import MetricToolScopeV2
from app.application.investigator_runtime import InvestigatorUsageDelta
from app.adapters.persistence.jobs import JobRepository
from app.domains.investigations.runtime import (
    EvidenceFindingV2,
    EvidenceSnapshotV2,
    InvestigationActivityV2,
    InvestigationActionV2,
    InvestigationReportV2,
    InvestigationRunState,
    InvestigationVerdict,
    MetricObservationV2,
)
from app.api.v1.operator_witness import OperatorWitness
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _store(tmp_path: Path) -> tuple[SqlAlchemyUnifiedInvestigationStore, object]:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "v2.db"))
    upgrade_database(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                """
                INSERT INTO operational_occurrence (
                  id,incident_id,occurrence_no,source_id,group_key,title,signal_state,
                  signal_severity,response_state,response_priority,resolution_code,
                  duplicate_of_occurrence_id,service_id,primary_alertname,
                  aggregation_rule_id,group_labels_json,assignment_origin,member_count,
                  evidence_completeness,detected_at,source_started_at,ack_sla_seconds,
                  ack_sla_due_at,acknowledged_at,first_investigating_at,mitigated_at,
                  resolved_at,latest_activity_at,version
                ) VALUES (
                  1,1,1,'source-a','group','Checkout incident','FIRING','warning',
                  'UNACKNOWLEDGED','P3',NULL,NULL,NULL,'CheckoutErrorRateHigh',NULL,
                  '{}','UNMAPPED',1,'COMPLETE',:now,:now,900,NULL,NULL,NULL,NULL,NULL,:now,1
                )
                """
            ),
            {"now": datetime(2026, 9, 4)},
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    return SqlAlchemyUnifiedInvestigationStore(
        create_session_factory(engine), jobs=JobRepository()
    ), engine


def _snapshot(investigation_id: str = "run-v2") -> EvidenceSnapshotV2:
    return EvidenceSnapshotV2(
        investigation_id=investigation_id,
        occurrence_id=1,
        alert_evidence=(
            {
                "evidence_id": "alert-1",
                "alertname": "CheckoutErrorRateHigh",
                "summary": "结账错误率升高",
            },
        ),
        metric_evidence=(
            MetricObservationV2(
                "metric-1",
                "checkout_http_error_ratio",
                "DATA",
                {"latest": 0.08, "minimum": 0.08, "maximum": 0.08},
                ((1.0, "0.08"),),
            ),
        ),
        degraded_domains=(),
    )


def test_v2_claim_is_idempotent_and_does_not_write_legacy_facts(tmp_path: Path) -> None:
    store, engine = _store(tmp_path)
    now = datetime(2026, 9, 4, tzinfo=UTC)

    first = store.create(
        occurrence_id=1,
        request_key="unified-start-1",
        provider_profile_id=None,
        model_channel_id=None,
        model_revision=None,
        request_id="request-1",
        source_ip="127.0.0.1",
        snapshot=_snapshot(),
        source_id="source-a",
        connection_version=None,
        tool_scope=(
            MetricToolScopeV2(
                "checkout_http_error_ratio",
                "结账错误率",
                "HTTP 错误比例",
                "ratio",
                "checkout_http_error_ratio",
            ),
        ),
        queue_model=False,
        now=now,
    )
    replay = store.create(
        occurrence_id=1,
        request_key="unified-start-1",
        provider_profile_id=None,
        model_channel_id=None,
        model_revision=None,
        request_id="request-1",
        source_ip="127.0.0.1",
        snapshot=_snapshot(),
        source_id="source-a",
        connection_version=None,
        tool_scope=(
            MetricToolScopeV2(
                "checkout_http_error_ratio",
                "结账错误率",
                "HTTP 错误比例",
                "ratio",
                "checkout_http_error_ratio",
            ),
        ),
        queue_model=False,
        now=now,
    )

    assert replay.id == first.id
    assert first.status is InvestigationRunState.DEGRADED
    assert first.snapshot.metric_evidence[0].evidence_id == "metric-1"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM investigation")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM evidence_snapshot_v2")).scalar_one() == 1
    engine.dispose()


def test_v2_worker_state_activity_usage_and_report_are_persisted(tmp_path: Path) -> None:
    store, engine = _store(tmp_path)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    created = store.create(
        occurrence_id=1,
        request_key="unified-start-2",
        provider_profile_id=None,
        model_channel_id="model-1",
        model_revision=3,
        request_id="request-2",
        source_ip="127.0.0.1",
        snapshot=_snapshot(),
        source_id="source-a",
        connection_version=1,
        tool_scope=(
            MetricToolScopeV2(
                "checkout_http_error_ratio",
                "结账错误率",
                "HTTP 错误比例",
                "ratio",
                "checkout_http_error_ratio",
            ),
        ),
        queue_model=True,
        now=now,
    )
    assert created.status is InvestigationRunState.QUEUED
    assert created.job_id is not None

    store.mark_running(created.id, now=now)
    live_observation = MetricObservationV2(
        "metric-live-1",
        "checkout_http_error_ratio",
        "DATA",
        {"latest": 0.09},
        ((2.0, "0.09"),),
    )
    store.record_observation(created.id, live_observation, now=now)
    assert store.get(created.id).observations == (live_observation,)
    report = InvestigationReportV2(
        summary_zh="当前错误率证据支持事件仍然存在，建议人工核对影响范围。",
        verdict=InvestigationVerdict.LIKELY_INCIDENT,
        confidence=0.75,
        findings=(
            EvidenceFindingV2("错误率持续偏高", "指标样本持续为非零值。", ("metric-1",)),
        ),
        recommended_actions=(
            InvestigationActionV2("核对状态码", "确认影响范围。", ("metric-1",)),
        ),
        missing_evidence_zh=("缺少接口级状态码分布。",),
        degraded_domains=(),
        evidence_gain=1,
    )
    store.complete(
        created.id,
        observations=(live_observation,),
        activities=(InvestigationActivityV2(1, "query_metric", "COMPLETED", "OK", ("metric-1",)),),
        report=report,
        request_count=2,
        tool_call_count=1,
        input_tokens=100,
        output_tokens=40,
        now=now,
    )

    current = store.get(created.id)
    assert current.status is InvestigationRunState.COMPLETED
    assert current.report == report
    assert current.request_count == 2
    assert current.tool_call_count == 1
    assert current.activities[0].kind == "query_metric"
    assert current.observations == (live_observation,)
    feedback = store.record_feedback(
        created.id,
        rating="ADOPTED",
        actor=OperatorWitness().actor(),
        now=now + timedelta(minutes=1),
    )
    assert feedback.sequence == 1
    assert store.get(created.id).feedback == feedback
    assert store.cleanup(now=now + timedelta(days=91), retention_days=90) == 1
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM investigation_feedback_v2")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM investigation_run_v2")
        ).scalar_one() == 0
    engine.dispose()


def test_canceled_run_cannot_be_overwritten_by_a_late_model_result(tmp_path: Path) -> None:
    store, engine = _store(tmp_path)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    created = store.create(
        occurrence_id=1,
        request_key="unified-cancel-1",
        provider_profile_id=None,
        model_channel_id="model-1",
        model_revision=1,
        request_id="request-cancel",
        source_ip="127.0.0.1",
        snapshot=_snapshot(),
        source_id="source-a",
        connection_version=1,
        tool_scope=(
            MetricToolScopeV2(
                "checkout_http_error_ratio",
                "结账错误率",
                "HTTP 错误比例",
                "ratio",
                "checkout_http_error_ratio",
            ),
        ),
        queue_model=True,
        now=now,
    )
    store.mark_running(created.id, now=now)
    store.cancel(created.id, now=now)
    report = InvestigationReportV2(
        summary_zh="这份迟到结果不应覆盖人工停止状态。",
        verdict=InvestigationVerdict.INCONCLUSIVE,
        confidence=0.1,
        findings=(),
        recommended_actions=(),
        missing_evidence_zh=(),
        degraded_domains=(),
        evidence_gain=0,
    )

    store.complete(
        created.id,
        observations=(),
        activities=(),
        report=report,
        request_count=1,
        tool_call_count=0,
        input_tokens=1,
        output_tokens=1,
        now=now,
    )

    current = store.get(created.id)
    assert current.status is InvestigationRunState.CANCELED
    assert current.report is None
    engine.dispose()


def test_canceled_run_keeps_incremental_usage_from_a_returned_model_response(
    tmp_path: Path,
) -> None:
    store, engine = _store(tmp_path)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    created = store.create(
        occurrence_id=1,
        request_key="unified-cancel-usage-1",
        provider_profile_id=None,
        model_channel_id="model-1",
        model_revision=1,
        request_id="request-cancel-usage",
        source_ip="127.0.0.1",
        snapshot=_snapshot(),
        source_id="source-a",
        connection_version=1,
        tool_scope=(
            MetricToolScopeV2(
                "checkout_http_error_ratio",
                "结账错误率",
                "HTTP 错误比例",
                "ratio",
                "checkout_http_error_ratio",
            ),
        ),
        queue_model=True,
        now=now,
    )
    store.mark_running(created.id, now=now)
    store.cancel(created.id, now=now)

    store.record_usage(
        created.id,
        InvestigatorUsageDelta(1, 1, 17, 9),
        now=now,
    )

    current = store.get(created.id)
    assert current.status is InvestigationRunState.CANCELED
    assert (
        current.request_count,
        current.tool_call_count,
        current.input_tokens,
        current.output_tokens,
    ) == (1, 1, 17, 9)
    engine.dispose()
