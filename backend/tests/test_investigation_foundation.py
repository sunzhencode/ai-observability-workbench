"""D1 frozen-scope, typed evidence and explicit-start contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.application.observability import ModelChannelInput, MonitoringConnectionDraft
from app.application.evidence_expansion import RunEvidenceExpansion
from app.application.incidents import IncidentResponseCommands
from app.application.investigations import StartInvestigation
from app.application.unified_investigations import MetricToolScopeV2
from app.application.sources import EndpointDraft, SourceDraft
from app.api.v1.operator_witness import OperatorWitness
from app.bootstrap import create_job_platform_app
from app.domains.incidents.actors import SystemActor
from app.domains.incidents.response import ResolutionCode, ResponseAction
from app.domains.investigations.models import (
    EvidenceBriefV1,
    InvestigationCanceledV1,
    InvestigationDegradationV1,
    InvestigationFactV1,
    InvestigationPhaseTransitionV1,
    MetricEmptyObservationV1,
    MetricObservationV1,
    SimilarHistoryObservationV1,
    summarize_series,
)
from app.domains.investigations.planner import LabelRejectionV1, PlannerStepV1
from app.domains.investigations.runtime import EvidenceSnapshotV2, MetricObservationV2
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations

UTC = timezone.utc


def _candidate(tmp_path: Path, *, model_fake_mode: bool = False):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "candidate.db",
        master_key_path=key,
        cursor_secret=b"investigation-cursor-key-at-least-32-bytes",
        model_fake_mode=model_fake_mode,
    )
    resources.sources.create_source(
        SourceDraft(
            id="src-a",
            name="Primary AM",
            endpoints=(EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )
    snapshot = resources.sources.load_snapshot("src-a", expected_version=1)
    raw = {
        "fingerprint": "investigation-member",
        "labels": {"alertname": "CheckoutLatency", "severity": "warning"},
        "annotations": {"summary": "checkout latency elevated"},
        "startsAt": "2026-08-24T00:00:00Z",
        "generatorURL": "https://prom.invalid/graph?g0.expr=histogram_quantile%280.99%2Cfoo%29%3E1",
    }
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", (raw,), 2),)
    )
    resources.sources.apply_collection(
        snapshot, outcome, observed_at=datetime(2026, 8, 24, 1, tzinfo=UTC)
    )
    return resources


def _activate_thanos(resources) -> None:
    resources.observability.save_monitoring_connection(
        MonitoringConnectionDraft("src-a", "THANOS", "http://thanos.internal"),
        expected_version=None,
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    resources.observability.record_monitoring_test(
        "src-a", "THANOS", expected_version=1, ok=True, safe_error_code="OK",
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )


def _activate_model(resources) -> None:
    channel = resources.observability.create_model_channel(
        ModelChannelInput(
            "Test model",
            "OPENAI_COMPATIBLE",
            "https://model.invalid",
            "test-model",
            api_key_action="REPLACE",
            api_key_value="synthetic-test-key",
        ),
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    resources.observability.record_model_test(
        channel.id, expected_revision=channel.revision_no, ok=True, safe_error_code="OK",
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    resources.observability.activate_model_channel(
        channel.id, expected_revision=channel.revision_no,
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )


def _reopen(tmp_path: Path, reader):
    return create_job_platform_app(
        database_path=tmp_path / "candidate.db",
        master_key_path=tmp_path / "master.key",
        cursor_secret=b"investigation-cursor-key-at-least-32-bytes",
        thanos_factory=lambda _url, _secret: reader,
    )


def _duplicate_alerts(resources, total: int) -> None:
    with resources.engine.begin() as connection:
        for index in range(2, total + 1):
            connection.execute(text("""
                INSERT INTO alert (
                  source_id,upstream_fingerprint,alertname,severity,cluster,labels_json,
                  annotations_json,raw_json,origin,evidence_completeness,source_state,
                  missing_since_at,starts_at,last_seen_at,endpoint_positions_json,incident_id
                ) SELECT source_id,:fingerprint,alertname,severity,cluster,labels_json,
                  annotations_json,raw_json,origin,evidence_completeness,source_state,
                  missing_since_at,starts_at,last_seen_at,endpoint_positions_json,incident_id
                FROM alert WHERE upstream_fingerprint='investigation-member'
            """), {"fingerprint": f"investigation-member-{index}"})


def test_page_read_does_not_create_investigation_and_start_is_idempotent(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        path = f"/api/v1/occurrences/{occurrence_id}/investigations"
        assert client.get(path).json() == []
        started = client.post(path, headers={"Idempotency-Key": "investigation-start-0001"})
        assert started.status_code == 200
        assert started.json()["status"] == "EVIDENCE_ONLY"
        assert started.json()["degraded_domains"] == ["metrics", "service"]
        assert {item["code"] for item in started.json()["degradations"]} == {
            "SOURCE_UNAVAILABLE",
            "SERVICE_UNMAPPED",
        }
        assert started.json()["model_cost_status"] == "NOT_INCURRED"
        assert started.json()["usage"]["model_execution_mode"] == "EXTERNAL"
        assert "l3_series" not in started.text
        replay = client.post(path, headers={"Idempotency-Key": "investigation-start-0001"})
        assert replay.json()["id"] == started.json()["id"]
        assert len(client.get(path).json()) == 1
    resources.engine.dispose()


def test_fake_runtime_is_persisted_separately_from_external_model_usage(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path, model_fake_mode=True)
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        response = client.post(
            f"/api/v1/occurrences/{occurrence_id}/investigations",
            headers={"Idempotency-Key": "investigation-fake-mode-0001"},
        )
    assert response.status_code == 200
    assert response.json()["usage"]["model_execution_mode"] == "FAKE"
    resources.engine.dispose()


def test_system_actor_cannot_claim_an_investigation(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    occurrence = resources.incidents.list_operational_occurrences(
        view="ALL", source_ids=(), signal_states=(), cursor=None, limit=10,
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    ).items[0]
    with pytest.raises(PermissionError, match="INTERACTIVE_OPERATOR_REQUIRED"):
        resources.investigations.claim(
            occurrence_id=occurrence.id, incident_id=occurrence.incident_id,
            request_key="system-start-denied", member_scope=(),
            occurrence_version=occurrence.version,
            actor=SystemActor("AI"),  # type: ignore[arg-type]
            request_id="worker", source_ip="local",
            now=datetime(2026, 8, 24, 1, tzinfo=UTC),
        )
    resources.engine.dispose()


def test_expired_start_claim_is_reaped_and_a_new_request_can_continue(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    occurrence = resources.incidents.list_operational_occurrences(
        view="ALL", source_ids=(), signal_states=(), cursor=None, limit=10,
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    ).items[0]
    first = resources.investigations.claim(
        occurrence_id=occurrence.id, incident_id=occurrence.incident_id,
        request_key="claim-crash-0001", member_scope=(), actor=OperatorWitness().actor(),
        occurrence_version=occurrence.version,
        request_id="request-one", source_ip="local",
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    with resources.engine.begin() as connection:
        connection.execute(
            text("UPDATE investigation SET claim_expires_at='2026-08-24 00:00:00' WHERE id=:id"),
            {"id": first.investigation.id},
        )
    second = resources.investigations.claim(
        occurrence_id=occurrence.id, incident_id=occurrence.incident_id,
        request_key="claim-recovery-0001", member_scope=(), actor=OperatorWitness().actor(),
        occurrence_version=occurrence.version,
        request_id="request-two", source_ip="local",
        now=datetime(2026, 8, 24, 1, 1, tzinfo=UTC),
    )
    assert second.replayed is False
    assert second.investigation.id != first.investigation.id
    assert resources.investigations.get(first.investigation.id).status == "FAILED"
    resources.engine.dispose()


def test_empty_observation_has_distinct_code_and_l3_is_not_l2() -> None:
    empty = MetricEmptyObservationV1("metric-a", "alert-a", "主曲线")
    assert empty.code == "EMPTY_NO_DATA"
    assert "窗口内没有指标数据" in empty.message
    assert "不可达" not in empty.message
    l3 = tuple(
        {"metric": {"instance": "a"}, "values": [[float(index), str(index)]]}
        for index in range(30)
    )
    summary, l2 = summarize_series(l3)
    assert summary["point_count"] == 30
    assert len(l2) == 20
    assert len(l3) == 30


def test_investigation_trajectory_accepts_only_closed_typed_facts() -> None:
    assert set(get_args(InvestigationFactV1)) == {
        InvestigationPhaseTransitionV1,
        EvidenceBriefV1,
        MetricObservationV1,
        MetricEmptyObservationV1,
        SimilarHistoryObservationV1,
        InvestigationDegradationV1,
        InvestigationCanceledV1,
        PlannerStepV1,
        LabelRejectionV1,
    }
    for fact_type in get_args(InvestigationFactV1):
        assert "kind" not in fact_type.__dataclass_fields__
        assert "payload" not in fact_type.__dataclass_fields__


def test_snapshot_freezes_all_members_and_makes_twenty_detail_limit_visible(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    _duplicate_alerts(resources, 25)
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        response = client.post(
            f"/api/v1/occurrences/{occurrence_id}/investigations",
            headers={"Idempotency-Key": "investigation-coverage-0001"},
        )
        assert response.json()["member_total"] == 25
        assert response.json()["member_detailed"] == 20
    with resources.engine.connect() as connection:
        snapshot = connection.execute(text(
            "SELECT snapshot_json FROM initial_investigation_snapshot"
        )).scalar_one()
    assert '"summarized_count":5' in snapshot
    assert snapshot.count('"alert_ref"') == 20
    assert "generatorURL" not in snapshot
    resources.engine.dispose()


def test_snapshot_is_append_only_and_terminal_run_expires_after_ninety_days(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        started = client.post(
            f"/api/v1/occurrences/{occurrence_id}/investigations",
            headers={"Idempotency-Key": "investigation-retention-0001"},
        ).json()
    with pytest.raises(IntegrityError, match="append-only"):
        with resources.engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM initial_investigation_snapshot WHERE investigation_id=:id"
            ), {"id": started["id"]})
    created_at = datetime.fromisoformat(started["created_at"].replace("Z", "+00:00"))
    deleted = resources.investigations.cleanup(
        now=created_at + timedelta(days=91), retention_days=90
    )
    assert deleted == 1
    with pytest.raises(LookupError, match="INVESTIGATION_NOT_FOUND"):
        resources.investigations.get(started["id"])
    resources.engine.dispose()


def test_empty_and_unavailable_are_persisted_as_different_fact_types(tmp_path: Path) -> None:
    mode = {"value": "empty"}

    class Reader:
        async def metric_names(self, *_args):
            return ("histogram_quantile",)

        async def query_range(self, *_args):
            if mode["value"] == "unavailable":
                error = RuntimeError("private upstream detail")
                error.code = "THANOS_UNAVAILABLE"  # type: ignore[attr-defined]
                raise error
            return {"resultType": "matrix", "result": []}

    resources = _candidate(tmp_path)
    _activate_thanos(resources)
    resources.engine.dispose()
    resources = _reopen(tmp_path, Reader())
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        path = f"/api/v1/occurrences/{occurrence_id}/investigations"
        empty_result = client.post(path, headers={"Idempotency-Key": "investigation-empty-0001"})
        assert empty_result.json()["empty_metric_facts"] == 1
        assert "metrics" not in empty_result.json()["degraded_domains"]
        mode["value"] = "unavailable"
        unavailable = client.post(path, headers={"Idempotency-Key": "investigation-unavailable-0001"})
        assert unavailable.json()["empty_metric_facts"] == 0
        assert "metrics" in unavailable.json()["degraded_domains"]
        assert unavailable.json()["degradations"][0]["code"] == "SOURCE_UNAVAILABLE"
    with resources.engine.connect() as connection:
        empty_codes = connection.execute(text("SELECT code FROM metric_empty_observation_v1")).scalars().all()
        degradation_codes = connection.execute(text("SELECT code FROM investigation_degradation_v1 WHERE domain='metrics'")).scalars().all()
    assert empty_codes == ["EMPTY_NO_DATA"]
    assert "SOURCE_UNAVAILABLE" in degradation_codes
    assert set(empty_codes).isdisjoint(degradation_codes)
    resources.engine.dispose()


def test_successful_fact_and_active_model_create_durable_handoff_without_calling_model(tmp_path: Path) -> None:
    class Reader:
        async def query_range(self, *_args):
            return {"resultType": "matrix", "result": [{"metric": {"instance": "checkout"}, "values": [[1, "2"]]}]}

    resources = _candidate(tmp_path)
    _activate_thanos(resources)
    channel = resources.observability.create_model_channel(
        ModelChannelInput("Test model", "OPENAI_COMPATIBLE", "https://model.invalid", "test-model"),
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    resources.observability.record_model_test(
        channel.id, expected_revision=channel.revision_no, ok=True, safe_error_code="OK",
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    resources.observability.activate_model_channel(
        channel.id, expected_revision=channel.revision_no,
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    )
    resources.engine.dispose()
    resources = _reopen(tmp_path, Reader())
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        response = client.post(
            f"/api/v1/occurrences/{occurrence_id}/investigations",
            headers={"Idempotency-Key": "investigation-job-0001"},
        )
        assert response.status_code == 202
        assert response.json()["status"] == "QUEUED"
        assert response.json()["successful_metric_facts"] == 1
        assert response.json()["job_id"]
        assert response.json()["model_cost_status"] == "UNKNOWN"
    resources.engine.dispose()


def test_zero_hop_deadline_keeps_six_query_budget_and_two_read_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    maximum = 0

    class SlowReader:
        async def query_range(self, *_args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.2)
                return {"resultType": "matrix", "result": []}
            finally:
                active -= 1

    resources = _candidate(tmp_path)
    _duplicate_alerts(resources, 6)
    _activate_thanos(resources)
    resources.engine.dispose()
    monkeypatch.setattr("app.application.investigations.MAX_ZERO_HOP_WALL_CLOCK_SECONDS", 0.05)
    resources = _reopen(tmp_path, SlowReader())
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        response = client.post(
            f"/api/v1/occurrences/{occurrence_id}/investigations",
            headers={"Idempotency-Key": "investigation-deadline-0001"},
        )
        assert response.status_code == 200
        assert response.json()["query_planned"] == 6
        assert response.json()["query_completed"] == 0
    with resources.engine.connect() as connection:
        deadline_count = connection.execute(text(
            "SELECT COUNT(*) FROM investigation_degradation_v1 WHERE code='ZERO_HOP_DEADLINE_REACHED'"
        )).scalar_one()
    assert deadline_count == 6
    assert maximum == 2
    resources.engine.dispose()


def test_resolve_requests_same_transaction_cancel_and_planner_stops_before_model(
    tmp_path: Path,
) -> None:
    class Reader:
        async def metric_names(self, *_args):
            return ("histogram_quantile",)

        async def query_range(self, *_args):
            return {
                "resultType": "matrix",
                "result": [{"metric": {"instance": "checkout"}, "values": [[1, "2"]]}],
            }

    resources = _candidate(tmp_path)
    _activate_thanos(resources)
    _activate_model(resources)
    occurrence = resources.incidents.list_operational_occurrences(
        view="ALL", source_ids=(), signal_states=(), cursor=None, limit=10,
        now=datetime(2026, 8, 24, 1, tzinfo=UTC),
    ).items[0]
    started = asyncio.run(StartInvestigation(
        store=resources.investigations,
        incidents=resources.incidents,
        observability=resources.observability,
        thanos_factory=lambda _url, _secret: Reader(),
    ).execute(
        occurrence.id,
        request_key="resolve-cancel-0001",
        actor=OperatorWitness().actor(),
        request_id="start-request",
        source_ip="local",
        now=datetime(2026, 8, 24, 1, 1, tzinfo=UTC),
    ))
    assert started.status == "QUEUED"
    model = resources.observability.active_model_channel()
    assert model is not None and model.provider_profile_id is not None
    unified = resources.unified_investigations.create(
        occurrence_id=occurrence.id,
        request_key="resolve-cancel-v2-0001",
        provider_profile_id=model.provider_profile_id,
        model_channel_id=model.id,
        model_revision=model.revision_no,
        request_id="start-v2-request",
        source_ip="local",
        snapshot=EvidenceSnapshotV2(
            investigation_id="pending",
            occurrence_id=occurrence.id,
            alert_evidence=({"evidence_id": "alert-v2-test"},),
            metric_evidence=(
                MetricObservationV2(
                    "metric-v2-test", "test_metric", "DATA", {"latest": 1.0}, ()
                ),
            ),
            degraded_domains=(),
        ),
        source_id="src-a",
        connection_version=1,
        tool_scope=(
            MetricToolScopeV2(
                "test_metric", "测试指标", "测试只读指标", "", "test_metric"
            ),
        ),
        queue_model=True,
        model_execution_mode="EXTERNAL",
        now=datetime(2026, 8, 24, 1, 1, tzinfo=UTC),
    )
    assert unified.status.value == "QUEUED"

    commands = IncidentResponseCommands(resources.incidents)
    handling = commands.execute(
        occurrence.id,
        action=ResponseAction.start_handling("开始人工核对"),
        actor=OperatorWitness().actor(),
        expected_version=occurrence.version,
        idempotency_key="resolve-cancel-handling",
        request_id="handling-request",
        source_ip="local",
        now=datetime(2026, 8, 24, 1, 2, tzinfo=UTC),
    )
    commands.execute(
        occurrence.id,
        action=ResponseAction.resolve(
            ResolutionCode.FALSE_POSITIVE, reason="人工确认该信号无需继续处理"
        ),
        actor=OperatorWitness().actor(),
        expected_version=handling.version,
        idempotency_key="resolve-cancel-resolution",
        request_id="resolve-request",
        source_ip="local",
        now=datetime(2026, 8, 24, 1, 3, tzinfo=UTC),
    )
    requested = resources.investigations.get(started.id)
    assert requested.status == "QUEUED"
    assert requested.cancel_requested_at is not None
    canceled_v2 = resources.unified_investigations.get(unified.id)
    assert canceled_v2.status.value == "CANCELED"
    assert canceled_v2.safe_error_code == "OCCURRENCE_RESOLVED"

    model_created = False

    def model_factory(_kind, _config):
        nonlocal model_created
        model_created = True
        raise AssertionError("a canceled investigation must not construct a model client")

    asyncio.run(RunEvidenceExpansion(
        store=resources.investigations,
        observability=resources.observability,
        thanos_factory=lambda _url, _secret: Reader(),
        model_client_factory=model_factory,
    ).execute(started.id, now=datetime(2026, 8, 24, 1, 4, tzinfo=UTC)))
    canceled = resources.investigations.get(started.id)
    assert canceled.phase == "CANCELED"
    assert canceled.termination_reason == "OCCURRENCE_RESOLVED"
    assert model_created is False
    resources.engine.dispose()
