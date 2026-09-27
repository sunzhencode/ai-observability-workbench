"""Deterministic similar-occurrence scoring and frozen-history contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.application.sources import EndpointDraft, SourceDraft
from app.bootstrap import create_job_platform_app
from app.domains.incidents.similar_history import (
    SimilarityProfileV1,
    score_similar_occurrence,
)
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations

UTC = timezone.utc


def test_scoring_is_explainable_and_does_not_award_missing_service() -> None:
    current = SimilarityProfileV1(
        service_id=7,
        primary_alertname="CheckoutErrorRateHigh",
        aggregation_rule_id=3,
        group_labels={"cluster": "prod", "namespace": "checkout"},
    )
    candidate = SimilarityProfileV1(
        service_id=7,
        primary_alertname="CheckoutErrorRateHigh",
        aggregation_rule_id=3,
        group_labels={"cluster": "prod", "namespace": "checkout"},
    )
    result = score_similar_occurrence(current, candidate)
    assert result.score == 90
    assert [(item.kind, item.points) for item in result.reasons] == [
        ("SERVICE", 40),
        ("PRIMARY_ALERTNAME", 25),
        ("AGGREGATION_RULE", 15),
        ("GROUP_LABEL", 5),
        ("GROUP_LABEL", 5),
    ]

    unmapped = SimilarityProfileV1(
        service_id=None,
        primary_alertname="CheckoutErrorRateHigh",
        aggregation_rule_id=3,
        group_labels={"cluster": "prod", "namespace": "checkout"},
    )
    fallback = score_similar_occurrence(unmapped, unmapped)
    assert fallback.score == 50
    assert all(item.kind != "SERVICE" for item in fallback.reasons)


def _candidate(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "candidate.db",
        master_key_path=key,
        cursor_secret=b"similar-history-cursor-key-at-least-32-bytes",
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
    outcome = merge_endpoint_observations(
        (
            EndpointObservation(
                snapshot.endpoints[0],
                "SUCCESS",
                (
                    {
                        "fingerprint": "similar-history-member",
                        "labels": {
                            "alertname": "CheckoutErrorRateHigh",
                            "severity": "warning",
                            "cluster": "prod",
                        },
                        "annotations": {"summary": "checkout errors elevated"},
                        "startsAt": "2026-08-24T00:00:00Z",
                    },
                ),
                2,
            ),
        )
    )
    resources.sources.apply_collection(
        snapshot, outcome, observed_at=datetime(2026, 8, 24, 1, tzinfo=UTC)
    )
    return resources


def _seed_resolved_predecessor(resources) -> tuple[int, int]:
    """Turn the first root into history and clone its frozen profile as current."""
    with resources.engine.begin() as connection:
        candidate_id = int(
            connection.scalar(text("SELECT id FROM operational_occurrence"))
        )
        connection.execute(
            text(
                "UPDATE operational_occurrence SET response_state='RESOLVED', "
                "resolution_code='FIXED', resolved_at='2026-08-24 02:00:00.000000', "
                "latest_activity_at='2026-08-24 02:00:00.000000', service_id=7 "
                "WHERE id=:id"
            ),
            {"id": candidate_id},
        )
        connection.execute(
            text(
                "INSERT INTO operational_occurrence ("
                "incident_id,occurrence_no,source_id,group_key,title,signal_state,"
                "signal_severity,response_state,response_priority,resolution_code,"
                "duplicate_of_occurrence_id,service_id,assignment_origin,member_count,"
                "evidence_completeness,detected_at,source_started_at,ack_sla_seconds,"
                "ack_sla_due_at,acknowledged_at,first_investigating_at,mitigated_at,"
                "resolved_at,latest_activity_at,version,primary_alertname,"
                "aggregation_rule_id,group_labels_json) "
                "SELECT incident_id,2,source_id,group_key,title,'FIRING',signal_severity,"
                "'UNACKNOWLEDGED',response_priority,NULL,NULL,service_id,assignment_origin,"
                "member_count,evidence_completeness,'2026-08-25 01:00:00.000000',"
                "source_started_at,ack_sla_seconds,'2026-08-25 01:15:00.000000',NULL,NULL,"
                "NULL,NULL,'2026-08-25 01:00:00.000000',1,primary_alertname,"
                "aggregation_rule_id,group_labels_json "
                "FROM operational_occurrence WHERE id=:id"
            ),
            {"id": candidate_id},
        )
        current_id = int(connection.scalar(text("SELECT max(id) FROM operational_occurrence")))
        connection.execute(text("UPDATE incident SET occurrence_no=2"))
        connection.execute(
            text(
                "INSERT INTO incident_timeline_entry (occurrence_id,sequence,actor_type,event_type,"
                "summary,detail_json,request_id,source_ip,created_at) VALUES "
                "(:id,1,'INTERACTIVE_OPERATOR','OCCURRENCE_RESOLVED','已结束事件处理',"
                "'{\"reason\":\"回滚错误版本后恢复\",\"resolution_code\":\"FIXED\"}',"
                "'history-seed','local','2026-08-24 02:00:00.000000')"
            ),
            {"id": candidate_id},
        )
        connection.execute(
            text(
                "INSERT INTO incident_task (occurrence_id,title,description,due_at,runbook_link,"
                "status,result,version,created_at,updated_at) VALUES "
                "(:id,'回滚发布',NULL,NULL,NULL,'DONE','错误率恢复到阈值内',2,"
                "'2026-08-24 01:10:00.000000','2026-08-24 01:40:00.000000')"
            ),
            {"id": candidate_id},
        )
    return candidate_id, current_id


def test_api_returns_resolved_history_after_alert_retention_and_normal_empty_result(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    candidate_id, current_id = _seed_resolved_predecessor(resources)
    with resources.engine.begin() as connection:
        connection.execute(text("DELETE FROM alert"))

    with TestClient(resources.app) as client:
        response = client.get(f"/api/v1/occurrences/{current_id}/similar")
        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["occurrence_id"] == candidate_id
        assert payload[0]["score"] >= 50
        assert payload[0]["resolution_code"] == "FIXED"
        assert payload[0]["operator_conclusion"] == "回滚错误版本后恢复"
        assert "错误率恢复到阈值内" in payload[0]["task_outcome"]
        assert payload[0]["handling_duration_seconds"] == 7200
        assert any(item["kind"] == "PRIMARY_ALERTNAME" for item in payload[0]["match_reasons"])

        empty = client.get(f"/api/v1/occurrences/{candidate_id}/similar")
        assert empty.status_code == 200
        assert empty.json() == []
    resources.engine.dispose()


def test_history_is_source_scoped_sorted_and_capped_at_ten(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    candidate_id, current_id = _seed_resolved_predecessor(resources)
    same_source_ids: list[int] = []
    with resources.engine.begin() as connection:
        for offset in range(11):
            occurrence_no = offset + 3
            resolved_at = f"2026-08-24 {offset + 3:02d}:00:00.000000"
            connection.execute(
                text(
                    "INSERT INTO operational_occurrence ("
                    "incident_id,occurrence_no,source_id,group_key,title,signal_state,"
                    "signal_severity,response_state,response_priority,resolution_code,"
                    "duplicate_of_occurrence_id,service_id,assignment_origin,member_count,"
                    "evidence_completeness,detected_at,source_started_at,ack_sla_seconds,"
                    "ack_sla_due_at,acknowledged_at,first_investigating_at,mitigated_at,"
                    "resolved_at,latest_activity_at,version,primary_alertname,"
                    "aggregation_rule_id,group_labels_json) "
                    "SELECT incident_id,:occurrence_no,source_id,group_key,title,'RECOVERED',"
                    "signal_severity,'RESOLVED',response_priority,'FIXED',NULL,service_id,"
                    "assignment_origin,member_count,evidence_completeness,:resolved_at,"
                    "source_started_at,ack_sla_seconds,:resolved_at,:resolved_at,:resolved_at,"
                    ":resolved_at,:resolved_at,:resolved_at,1,primary_alertname,"
                    "aggregation_rule_id,group_labels_json "
                    "FROM operational_occurrence WHERE id=:candidate_id"
                ),
                {
                    "candidate_id": candidate_id,
                    "occurrence_no": occurrence_no,
                    "resolved_at": resolved_at,
                },
            )
            same_source_ids.append(
                int(connection.scalar(text("SELECT max(id) FROM operational_occurrence")))
            )
        connection.execute(
            text(
                "INSERT INTO operational_occurrence ("
                "incident_id,occurrence_no,source_id,group_key,title,signal_state,"
                "signal_severity,response_state,response_priority,resolution_code,"
                "duplicate_of_occurrence_id,service_id,assignment_origin,member_count,"
                "evidence_completeness,detected_at,source_started_at,ack_sla_seconds,"
                "ack_sla_due_at,acknowledged_at,first_investigating_at,mitigated_at,"
                "resolved_at,latest_activity_at,version,primary_alertname,"
                "aggregation_rule_id,group_labels_json) "
                "SELECT incident_id,99,'other-source',group_key,title,'RECOVERED',"
                "signal_severity,'RESOLVED',response_priority,'FIXED',NULL,service_id,"
                "assignment_origin,member_count,evidence_completeness,"
                "'2026-08-25 23:00:00.000000',source_started_at,ack_sla_seconds,"
                "'2026-08-25 23:00:00.000000','2026-08-25 23:00:00.000000',"
                "'2026-08-25 23:00:00.000000','2026-08-25 23:00:00.000000',"
                "'2026-08-25 23:00:00.000000','2026-08-25 23:00:00.000000',1,"
                "primary_alertname,aggregation_rule_id,group_labels_json "
                "FROM operational_occurrence WHERE id=:candidate_id"
            ),
            {"candidate_id": candidate_id},
        )
        other_source_id = int(
            connection.scalar(text("SELECT max(id) FROM operational_occurrence"))
        )

    with TestClient(resources.app) as client:
        response = client.get(f"/api/v1/occurrences/{current_id}/similar")
    assert response.status_code == 200
    returned_ids = [item["occurrence_id"] for item in response.json()]
    assert returned_ids == list(reversed(same_source_ids[-10:]))
    assert other_source_id not in returned_ids
    resources.engine.dispose()


def test_investigation_freezes_typed_similar_history_without_planner_tool(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    candidate_id, current_id = _seed_resolved_predecessor(resources)
    with TestClient(resources.app) as client:
        started = client.post(
            f"/api/v1/occurrences/{current_id}/investigations",
            headers={"Idempotency-Key": "similar-history-investigation-0001"},
        )
        assert started.status_code == 200
        assert "history" not in started.json()["degraded_domains"]
        assert all(
            item["action"] != "QUERY_SIMILAR_HISTORY"
            for item in started.json()["planner_steps"]
        )
        investigation_id = started.json()["id"]
    with resources.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT occurrence_id,score FROM similar_history_observation_v1 "
                "WHERE investigation_id=:id"
            ),
            {"id": investigation_id},
        ).one()
        assert tuple(row) == (candidate_id, 65)
        snapshot = str(
            connection.scalar(
                text(
                    "SELECT snapshot_json FROM initial_investigation_snapshot "
                    "WHERE investigation_id=:id"
                ),
                {"id": investigation_id},
            )
        )
        assert '"history":{"matches":[{' in snapshot
        assert '"status":"AVAILABLE"' in snapshot
    resources.engine.dispose()
