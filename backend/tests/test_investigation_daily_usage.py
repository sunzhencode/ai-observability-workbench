"""Platform-only UTC daily usage rollup contracts."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.bootstrap import create_job_platform_app


UTC = timezone.utc


def _candidate(tmp_path: Path, *, model_fake_mode: bool = False):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    return create_job_platform_app(
        database_path=tmp_path / "candidate.db",
        master_key_path=key,
        cursor_secret=b"daily-usage-cursor-key-at-least-32-bytes",
        model_fake_mode=model_fake_mode,
    )


def _insert_run(
    connection,
    *,
    identifier: str,
    created_at: str,
    updated_at: str,
    phase: str,
    termination_reason: str | None,
    execution_mode: str,
    planner_calls: int,
    analyst_calls: int,
    metric_queries: int,
    tokens: int,
    cost_status: str,
    p0_at: str,
    p1_at: str | None = None,
    p2_at: str | None = None,
    degradation: bool = False,
    feedback: str | None = None,
) -> None:
    occurrence_id = int(identifier[-2:], 16) + 1
    incident_id = int(identifier[-2:], 16) + 101
    connection.execute(
        text(
            """
            INSERT OR IGNORE INTO event_source (
              id,name,state,version,poll_interval_seconds,resolution_grace_seconds,
              max_parallel_endpoints,watchdog_enabled,watchdog_alertname,
              watchdog_identity_label,watchdog_missing_after_seconds,created_at,updated_at
            ) VALUES (
              'usage-source','Daily usage source','ENABLED',1,30,120,2,0,
              'Watchdog','cluster',180,:created_at,:created_at
            )
            """
        ),
        {"created_at": created_at},
    )
    connection.execute(
        text(
            """
            INSERT INTO incident (
              id,source_id,group_key,title,severity,source_state,freshness_state,
              handling_state,handling_version,aggregation_rule_id,aggregation_rule_version,
              group_labels_json,missing_labels_json,occurrence_no,occurrence_started_at,
              change_version,change_origin,updated_at
            ) VALUES (
              :incident_id,'usage-source',:group_key,'Daily usage fixture','warning',
              'FIRING','CURRENT','NEW',1,NULL,NULL,'{}','[]',1,:created_at,1,
              'LIVE_POLL',:updated_at
            )
            """
        ),
        {
            "incident_id": incident_id,
            "group_key": f"usage-{identifier}",
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO operational_occurrence (
              id,incident_id,occurrence_no,source_id,title,signal_state,signal_severity,
              response_state,response_priority,resolution_code,service_id,assignment_origin,
              detected_at,source_started_at,ack_sla_seconds,ack_sla_due_at,
              acknowledged_at,first_investigating_at,mitigated_at,resolved_at,
              latest_activity_at,version,group_key,member_count,evidence_completeness,
              duplicate_of_occurrence_id
            ) VALUES (
              :occurrence_id,:incident_id,1,'usage-source','Daily usage fixture','FIRING',
              'warning','UNACKNOWLEDGED','P1',NULL,NULL,'UNMAPPED',:created_at,:created_at,
              900,NULL,NULL,NULL,NULL,NULL,:updated_at,1,:group_key,1,'COMPLETE',NULL
            )
            """
        ),
        {
            "occurrence_id": occurrence_id,
            "incident_id": incident_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "group_key": f"usage-{identifier}",
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO investigation (
              id,occurrence_id,incident_id,request_key,status,phase,member_scope_json,
              member_total,member_detailed,query_planned,query_completed,
              successful_metric_facts,empty_metric_facts,degraded_domains_json,findings_json,
              job_id,model_cost_status,model_execution_mode,claim_owner,claim_expires_at,
              request_id,source_ip,cancel_requested_at,snapshot_occurrence_version,
              planner_rounds,accounted_tokens,metric_queries_total,termination_reason,
              analyst_calls,analyst_prompt_tokens,analyst_completion_tokens,created_at,updated_at
            ) VALUES (
              :id,:occurrence_id,:incident_id,:request_key,'EVIDENCE_ONLY',:phase,'[]',
              1,1,1,1,1,0,:degraded_domains,'[]',NULL,:cost_status,:execution_mode,NULL,NULL,
              :request_id,'127.0.0.1',NULL,1,:planner_calls,:tokens,:metric_queries,:termination_reason,
              :analyst_calls,0,0,:created_at,:updated_at
            )
            """
        ),
        {
            "id": identifier,
            "occurrence_id": occurrence_id,
            "incident_id": incident_id,
            "request_key": f"usage-{identifier}",
            "phase": phase,
            "degraded_domains": '["model"]' if degradation else "[]",
            "cost_status": cost_status,
            "execution_mode": execution_mode,
            "request_id": f"request-{identifier}",
            "planner_calls": planner_calls,
            "tokens": tokens,
            "metric_queries": metric_queries,
            "termination_reason": termination_reason,
            "analyst_calls": analyst_calls,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO evidence_brief_v1 (
              investigation_id,member_total,member_detailed,query_planned,query_completed,
              successful_metric_facts,empty_metric_facts,degraded_domains_json,findings_json,created_at
            ) VALUES (:id,1,1,1,1,1,0,'[]','[]',:created_at)
            """
        ),
        {"id": identifier, "created_at": p0_at},
    )
    if p1_at is not None:
        connection.execute(
            text(
                """
                INSERT INTO planner_step_v1 (
                  investigation_id,sequence,action,outcome,metric_name,window,aggregation,
                  label_filters_json,group_by_json,prompt_tokens,completion_tokens,
                  result_metric_names_json,descriptor_label_names_json,
                  descriptor_known_values_json,safe_code,created_at
                ) VALUES (
                  :id,1,'QUERY_METRIC','SOURCE_UNAVAILABLE','request_rate','1h','NONE',
                  '[]','[]',10,5,'[]','[]','{}','SOURCE_UNAVAILABLE',:created_at
                )
                """
            ),
            {"id": identifier, "created_at": p0_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO planner_step_v1 (
                  investigation_id,sequence,action,outcome,metric_name,window,aggregation,
                  label_filters_json,group_by_json,prompt_tokens,completion_tokens,
                  result_metric_names_json,descriptor_label_names_json,
                  descriptor_known_values_json,created_at
                ) VALUES (
                  :id,2,'QUERY_METRIC','COMPLETED','request_rate','1h','NONE',
                  '[]','[]',10,5,'["request_rate"]','[]','{}',:created_at
                )
                """
            ),
            {"id": identifier, "created_at": p1_at},
        )
    if p2_at is not None:
        connection.execute(
            text(
                """
                INSERT INTO analyst_result_v1 (
                  investigation_id,contract_revision,prompt_profile_revision,result_json,
                  prompt_tokens,completion_tokens,created_at
                ) VALUES (:id,1,1,'{}',20,10,:created_at)
                """
            ),
            {"id": identifier, "created_at": p2_at},
        )
    if degradation:
        connection.execute(
            text(
                """
                INSERT INTO investigation_degradation_v1 (
                  investigation_id,domain,code,message,created_at
                ) VALUES (:id,'model','ANALYST_CONTRACT_REJECTED','contract rejected',:created_at)
                """
            ),
            {"id": identifier, "created_at": updated_at},
        )
    if feedback is not None:
        connection.execute(
            text(
                """
                INSERT INTO investigation_feedback_v1 (
                  investigation_id,sequence,rating,initiator_kind,created_at
                ) VALUES (:id,1,'USEFUL','INTERACTIVE_OPERATOR',:created_at)
                """
            ),
            {"id": identifier, "created_at": updated_at},
        )
        if feedback != "USEFUL":
            connection.execute(
                text(
                    """
                    INSERT INTO investigation_feedback_v1 (
                      investigation_id,sequence,rating,initiator_kind,created_at
                    ) VALUES (:id,2,:rating,'INTERACTIVE_OPERATOR',:created_at)
                    """
                ),
                {"id": identifier, "rating": feedback, "created_at": updated_at},
            )


def test_daily_usage_is_idempotent_utc_platform_aggregation(tmp_path: Path) -> None:
    resources = _candidate(tmp_path, model_fake_mode=True)
    with resources.engine.begin() as connection:
        _insert_run(
            connection,
            identifier="0000000000000000000000000000000a",
            created_at="2026-08-29 23:59:50",
            updated_at="2026-08-29 23:59:55",
            phase="ANALYST_RESULT_READY",
            termination_reason="P2_VALID",
            execution_mode="FAKE",
            planner_calls=2,
            analyst_calls=1,
            metric_queries=3,
            tokens=500,
            cost_status="UNKNOWN",
            p0_at="2026-08-29 23:59:51",
            p1_at="2026-08-29 23:59:52",
            p2_at="2026-08-29 23:59:53",
            degradation=True,
            feedback="ADOPTED",
        )
        _insert_run(
            connection,
            identifier="0000000000000000000000000000000b",
            created_at="2026-08-30 00:00:00",
            updated_at="2026-08-30 00:00:01",
            phase="TERMINAL_EVIDENCE_ONLY",
            termination_reason=None,
            execution_mode="EXTERNAL",
            planner_calls=0,
            analyst_calls=0,
            metric_queries=1,
            tokens=0,
            cost_status="NOT_INCURRED",
            p0_at="2026-08-30 00:00:01",
        )
        _insert_run(
            connection,
            identifier="0000000000000000000000000000000c",
            created_at="2026-08-30 00:01:00",
            updated_at="2026-08-30 00:01:04",
            phase="EVIDENCE_EXPANDED",
            termination_reason="ANALYST_CONTRACT_REJECTED",
            execution_mode="EXTERNAL",
            planner_calls=1,
            analyst_calls=2,
            metric_queries=2,
            tokens=300,
            cost_status="UNKNOWN",
            p0_at="2026-08-30 00:01:01",
            p1_at="2026-08-30 00:01:02",
            degradation=True,
            feedback="NOT_USEFUL",
        )

    rows = resources.investigations.refresh_daily_usage(
        now=datetime(2026, 8, 30, 12, tzinfo=UTC)
    )
    by_day = {item.day_utc: item for item in rows}
    first = by_day[date(2026, 8, 29)]
    assert first.run_count == 1
    assert first.terminal_run_count == 1
    assert first.p2_valid_count == 1
    assert first.model_started_run_count == 1
    assert first.fake_model_started_run_count == 1
    assert first.external_model_started_run_count == 0
    assert first.planner_calls == 2
    assert first.analyst_calls == 1
    assert first.metric_queries == 3
    assert first.accounted_tokens == 500
    assert first.unknown_cost_run_count == 1
    assert first.feedback_response_count == 1
    assert first.feedback_adopted_count == 1
    assert first.degraded_run_count == 1
    assert (first.p0_mtti_count, first.p0_mtti_sum_ms) == (1, 1_000)
    assert (first.p1_mtti_count, first.p1_mtti_sum_ms) == (1, 2_000)
    assert (first.p2_mtti_count, first.p2_mtti_sum_ms) == (1, 3_000)

    second = by_day[date(2026, 8, 30)]
    assert second.run_count == 2
    assert second.terminal_run_count == 2
    assert second.evidence_only_count == 1
    assert second.contract_rejected_count == 1
    assert second.model_started_run_count == 1
    assert second.external_model_started_run_count == 1
    assert second.unknown_model_started_run_count == 0
    assert second.accounted_tokens == 300
    assert second.feedback_response_count == 1
    assert second.feedback_adopted_count == 0

    with resources.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE investigation SET accounted_tokens=350 "
                "WHERE id='0000000000000000000000000000000c'"
            )
        )
    rerun = resources.investigations.refresh_daily_usage(
        now=datetime(2026, 8, 30, 12, tzinfo=UTC)
    )
    assert {item.day_utc: item for item in rerun}[date(2026, 8, 30)].accounted_tokens == 350

    with TestClient(resources.app) as client:
        response = client.get(
            "/api/v1/investigation-usage/daily",
            params={"from_day": "2026-08-29", "to_day": "2026-08-30"},
        )
        invalid = client.get(
            "/api/v1/investigation-usage/daily",
            params={"from_day": "2025-08-29", "to_day": "2026-08-30"},
        )
    assert response.status_code == 200
    assert [item["day_utc"] for item in response.json()] == ["2026-08-29", "2026-08-30"]
    assert "source_ip" not in response.text
    assert "request_id" not in response.text
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVESTIGATION_USAGE_RANGE_INVALID"


def test_cleanup_finalizes_daily_usage_before_deleting_run_detail(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    with resources.engine.begin() as connection:
        _insert_run(
            connection,
            identifier="0000000000000000000000000000000d",
            created_at="2026-05-30 10:00:00",
            updated_at="2026-05-30 10:00:01",
            phase="TERMINAL_EVIDENCE_ONLY",
            termination_reason=None,
            execution_mode="UNKNOWN_LEGACY",
            planner_calls=0,
            analyst_calls=0,
            metric_queries=1,
            tokens=0,
            cost_status="NOT_INCURRED",
            p0_at="2026-05-30 10:00:01",
        )

    deleted = resources.investigations.cleanup(
        now=datetime(2026, 8, 30, 12, tzinfo=UTC), retention_days=90
    )
    assert deleted == 1
    rows = resources.investigations.list_daily_usage(
        from_day=date(2026, 5, 30), to_day=date(2026, 5, 30)
    )
    assert len(rows) == 1
    assert rows[0].run_count == 1
    assert rows[0].unknown_model_started_run_count == 0
    with resources.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM investigation WHERE id=:id"),
            {"id": "0000000000000000000000000000000d"},
        ).scalar_one() == 0


def test_daily_usage_purges_only_buckets_older_than_365_days(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    with resources.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO investigation_daily_usage_v1 (
                  day_utc,schema_revision,run_count,terminal_run_count,p2_valid_count,
                  evidence_only_count,canceled_count,contract_rejected_count,
                  dependency_failed_count,model_started_run_count,fake_model_started_run_count,
                  external_model_started_run_count,unknown_model_started_run_count,planner_calls,
                  analyst_calls,metric_queries,accounted_tokens,unknown_cost_run_count,
                  feedback_response_count,feedback_adopted_count,degraded_run_count,
                  p0_mtti_count,p0_mtti_sum_ms,p1_mtti_count,p1_mtti_sum_ms,
                  p2_mtti_count,p2_mtti_sum_ms,updated_at
                ) VALUES (
                  '2025-08-29',1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
                  0,0,0,0,0,0,'2026-08-30 00:00:00'
                ),(
                  '2025-08-30',1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
                  0,0,0,0,0,0,'2026-08-30 00:00:00'
                )
                """
            )
        )

    resources.investigations.refresh_daily_usage(
        now=datetime(2026, 8, 30, 12, tzinfo=UTC)
    )
    rows = resources.investigations.list_daily_usage(
        from_day=date(2025, 8, 29), to_day=date(2025, 8, 30)
    )
    assert [item.day_utc for item in rows] == [date(2025, 8, 30)]
