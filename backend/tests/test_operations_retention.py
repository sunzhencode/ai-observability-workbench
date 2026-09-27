"""Bounded, replay-safe Incident Operations retention contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app.adapters.persistence.retention import SqlAlchemyRetentionPersistence
from app.application.retention import (
    RetentionBatchResult,
    RetentionCoordinator,
    RetentionPass,
    RetentionPolicy,
)
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


class _FakeRetentionAdapter:
    def __init__(
        self,
        *,
        batch: RetentionBatchResult | None = None,
        rollup_error: Exception | None = None,
    ) -> None:
        self.batch = batch or RetentionBatchResult({}, False)
        self.rollup_error = rollup_error
        self.rollup_calls: list[datetime] = []
        self.cleanup_calls: list[tuple[datetime, RetentionPolicy]] = []

    def prepare_rollups(self, *, as_of: datetime, policy: RetentionPolicy) -> None:
        self.rollup_calls.append(as_of)
        if self.rollup_error is not None:
            raise self.rollup_error

    def cleanup_batch(
        self, *, as_of: datetime, policy: RetentionPolicy
    ) -> RetentionBatchResult:
        self.cleanup_calls.append((as_of, policy))
        return self.batch


class _NoopRollups:
    def refresh(self, **_kwargs: object) -> None:
        return None

    def finalize_retention_rollups(self, **_kwargs: object) -> None:
        return None


def _sqlite_retention(tmp_path: Path) -> tuple[object, SqlAlchemyRetentionPersistence]:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "retention.db")
    )
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    rollups = _NoopRollups()
    return engine, SqlAlchemyRetentionPersistence(
        sessions,
        analytics=rollups,
        investigations=rollups,
    )


def test_first_pass_rolls_up_before_cleanup_and_freezes_the_next_pass() -> None:
    now = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
    adapter = _FakeRetentionAdapter(
        batch=RetentionBatchResult({"platform_job": 500}, True)
    )

    result = RetentionCoordinator(adapter).run_pass(
        RetentionPass(as_of=now, pass_number=0, rollups_complete=False)
    )

    assert adapter.rollup_calls == [now]
    assert adapter.cleanup_calls == [(now, RetentionPolicy())]
    assert result.deleted_by_table == {"platform_job": 500}
    assert result.next_pass == RetentionPass(
        as_of=now,
        pass_number=1,
        rollups_complete=True,
    )


def test_rollup_failure_never_starts_detail_cleanup() -> None:
    adapter = _FakeRetentionAdapter(rollup_error=RuntimeError("ROLLUP_FAILED"))

    with pytest.raises(RuntimeError, match="ROLLUP_FAILED"):
        RetentionCoordinator(adapter).run_pass(
            RetentionPass(
                as_of=datetime(2026, 9, 10, tzinfo=UTC),
                pass_number=0,
                rollups_complete=False,
            )
        )

    assert adapter.cleanup_calls == []


def test_replayed_continuation_skips_rollup_and_daily_limit_stops_chaining() -> None:
    adapter = _FakeRetentionAdapter(
        batch=RetentionBatchResult({"source_poll_run": 500}, True)
    )
    policy = RetentionPolicy(max_passes_per_day=20)
    coordinator = RetentionCoordinator(adapter, policy=policy)
    now = datetime(2026, 9, 10, tzinfo=UTC)

    result = coordinator.run_pass(
        RetentionPass(as_of=now, pass_number=19, rollups_complete=True)
    )

    assert adapter.rollup_calls == []
    assert len(adapter.cleanup_calls) == 1
    assert result.has_more is True
    assert result.next_pass is None


def test_pass_payload_round_trip_keeps_frozen_utc_boundary_and_stable_key() -> None:
    retention_pass = RetentionPass(
        as_of=datetime(2026, 9, 10, 8, 45, tzinfo=UTC),
        pass_number=3,
        rollups_complete=True,
    )

    payload = retention_pass.to_payload()

    assert payload == {
        "as_of": "2026-09-10T08:45:00Z",
        "pass_number": 3,
        "rollups_complete": True,
    }
    assert RetentionPass.from_payload(payload) == retention_pass
    assert retention_pass.idempotency_key == "operations-retention-v3:2026-09-10:3"


@pytest.mark.parametrize(
    "overrides",
    (
        {"batch_rows": 501},
        {"max_passes_per_day": 21},
        {"runtime_days": 0},
    ),
)
def test_policy_rejects_values_that_expand_or_disable_code_owned_bounds(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(ValueError, match="RETENTION_POLICY_INVALID"):
        RetentionPolicy(**overrides)


@pytest.mark.parametrize(
    "retention_pass",
    (
        RetentionPass(
            as_of=datetime(2026, 9, 10),
            pass_number=0,
            rollups_complete=False,
        ),
        RetentionPass(
            as_of=datetime(2026, 9, 10, tzinfo=UTC),
            pass_number=-1,
            rollups_complete=False,
        ),
        RetentionPass(
            as_of=datetime(2026, 9, 10, tzinfo=UTC),
            pass_number=1,
            rollups_complete=False,
        ),
    ),
)
def test_pass_contract_rejects_ambiguous_or_untrusted_payloads(
    retention_pass: RetentionPass,
) -> None:
    with pytest.raises(ValueError, match="RETENTION_PASS_INVALID"):
        RetentionCoordinator(_FakeRetentionAdapter()).run_pass(retention_pass)


def test_sqlite_job_cleanup_is_bounded_and_preserves_non_terminal_work(
    tmp_path: Path,
) -> None:
    engine, persistence = _sqlite_retention(tmp_path)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    old = now - timedelta(days=31)
    old_stored = old.replace(tzinfo=None).isoformat(sep=" ")
    terminal_jobs = [
        {
            "id": f"terminal-{index:04d}",
            "kind": "test.done",
            "key": f"terminal-{index:04d}",
            "created": old_stored,
        }
        for index in range(501)
    ]
    pending_job = {
        "id": "pending-old",
        "kind": "test.pending",
        "key": "pending-old",
        "created": old_stored,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO platform_job (
                  id,kind,pool,subject_type,subject_id,state,payload_json,
                  payload_revision,idempotency_key,attempt,available_at,
                  lease_owner,lease_expires_at,started_at,finished_at,
                  safe_error_code,version,created_at,updated_at
                ) VALUES (
                  :id,:kind,'SOURCE','test',:id,'SUCCEEDED','{}',1,:key,1,
                  :created,NULL,NULL,:created,:created,NULL,2,:created,:created
                )
                """
            ),
            terminal_jobs,
        )
        connection.execute(
            text(
                """
                INSERT INTO platform_job (
                  id,kind,pool,subject_type,subject_id,state,payload_json,
                  payload_revision,idempotency_key,attempt,available_at,
                  lease_owner,lease_expires_at,started_at,finished_at,
                  safe_error_code,version,created_at,updated_at
                ) VALUES (
                  :id,:kind,'SOURCE','test',:id,'PENDING','{}',1,:key,0,
                  :created,NULL,NULL,NULL,NULL,NULL,1,:created,:created
                )
                """
            ),
            pending_job,
        )
        connection.execute(
            text(
                """
                INSERT INTO platform_event (
                  event_type,subject_type,subject_id,created_at
                ) VALUES ('job.succeeded','job',:id,:created)
                """
            ),
            [*terminal_jobs, pending_job],
        )

    first = persistence.cleanup_batch(as_of=now, policy=RetentionPolicy())
    assert first.deleted_by_table["platform_job"] == 500
    assert first.has_more is True
    second = persistence.cleanup_batch(as_of=now, policy=RetentionPolicy())
    assert second.deleted_by_table["platform_job"] == 1
    assert second.has_more is False

    with engine.connect() as connection:
        jobs = connection.execute(
            text("SELECT id,state FROM platform_job ORDER BY id")
        ).all()
        events = connection.execute(
            text("SELECT subject_id FROM platform_event ORDER BY subject_id")
        ).scalars().all()
    assert jobs == [("pending-old", "PENDING")]
    assert events == ["pending-old"]


def test_sqlite_poll_cleanup_keeps_every_recent_run_beyond_latest_200(
    tmp_path: Path,
) -> None:
    engine, persistence = _sqlite_retention(tmp_path)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    old = now - timedelta(days=8)
    recent = now - timedelta(days=1)
    old_stored = old.replace(tzinfo=None).isoformat(sep=" ")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO event_source (
                  id,name,state,version,poll_interval_seconds,
                  resolution_grace_seconds,max_parallel_endpoints,
                  watchdog_enabled,watchdog_alertname,watchdog_identity_label,
                  watchdog_missing_after_seconds,created_at,updated_at
                ) VALUES (
                  'source-retention','Retention Source','ENABLED',1,30,60,1,
                  0,'Watchdog','cluster',120,:created,:created
                )
                """
            ),
            {"created": old_stored},
        )
        runs = [
            {
                "id": index + 1,
                "started": (old + timedelta(seconds=index))
                .replace(tzinfo=None)
                .isoformat(sep=" "),
            }
            for index in range(205)
        ] + [
            {
                "id": index + 206,
                "started": (recent + timedelta(seconds=index))
                .replace(tzinfo=None)
                .isoformat(sep=" "),
            }
            for index in range(205)
        ]
        connection.execute(
            text(
                """
                INSERT INTO source_poll_run (
                  id,source_id,source_version,started_at,finished_at,
                  completeness,endpoint_total,endpoint_succeeded,alert_count,
                  safe_error_codes_json
                ) VALUES (
                  :id,'source-retention',1,:started,:started,'COMPLETE',1,1,0,'[]'
                )
                """
            ),
            runs,
        )
        connection.execute(
            text(
                """
                INSERT INTO endpoint_poll_result (
                  poll_run_id,endpoint_position,status,alert_count,duration_ms,
                  safe_error_code
                ) VALUES (:id,0,'SUCCEEDED',0,1,NULL)
                """
            ),
            runs,
        )

    result = persistence.cleanup_batch(as_of=now, policy=RetentionPolicy())

    assert result.deleted_by_table == {
        "endpoint_poll_result": 205,
        "source_poll_run": 205,
    }
    assert result.has_more is False
    with engine.connect() as connection:
        remaining = connection.scalar(text("SELECT COUNT(*) FROM source_poll_run"))
        oldest = connection.scalar(text("SELECT MIN(started_at) FROM source_poll_run"))
    assert remaining == 205
    assert str(oldest).startswith(recent.date().isoformat())


def test_sqlite_history_cleanup_uses_gate_and_never_deletes_unresolved_occurrence(
    tmp_path: Path,
) -> None:
    engine, persistence = _sqlite_retention(tmp_path)
    now = datetime(2026, 9, 10, tzinfo=UTC)
    old = (now - timedelta(days=366)).replace(tzinfo=None).isoformat(sep=" ")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO event_source (
                  id,name,state,version,poll_interval_seconds,
                  resolution_grace_seconds,max_parallel_endpoints,
                  watchdog_enabled,watchdog_alertname,watchdog_identity_label,
                  watchdog_missing_after_seconds,created_at,updated_at
                ) VALUES (
                  'source-history','History Source','ENABLED',1,30,60,1,
                  0,'Watchdog','cluster',120,:old,:old
                )
                """
            ),
            {"old": old},
        )
        connection.execute(
            text(
                """
                INSERT INTO incident (
                  id,source_id,group_key,title,severity,source_state,
                  freshness_state,aggregation_rule_id,aggregation_rule_version,
                  group_labels_json,missing_labels_json,occurrence_no,
                  occurrence_started_at,updated_at,handling_state,
                  handling_version,change_version,change_origin
                ) VALUES (
                  :id,'source-history',:key,:key,'WARNING',:source_state,
                  'FRESH',NULL,NULL,'{}','[]',1,:old,:old,'NEW',1,1,'LIVE_POLL'
                )
                """
            ),
            [
                {
                    "id": 1,
                    "key": "resolved-old",
                    "source_state": "RECOVERED",
                    "old": old,
                },
                {
                    "id": 2,
                    "key": "unresolved-old",
                    "source_state": "FIRING",
                    "old": old,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO alert (
                  id,source_id,upstream_fingerprint,alertname,severity,cluster,
                  labels_json,annotations_json,raw_json,source_state,
                  missing_since_at,starts_at,last_seen_at,
                  endpoint_positions_json,incident_id
                ) VALUES (
                  :id,'source-history',:fingerprint,'HistoryAlert','WARNING',
                  'local','{}','{}','{}',:source_state,NULL,:old,:old,'[0]',:id
                )
                """
            ),
            [
                {
                    "id": 1,
                    "fingerprint": "resolved-alert",
                    "source_state": "RECOVERED",
                    "old": old,
                },
                {
                    "id": 2,
                    "fingerprint": "active-alert",
                    "source_state": "FIRING",
                    "old": old,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO operational_occurrence (
                  id,incident_id,occurrence_no,source_id,title,signal_state,
                  signal_severity,response_state,response_priority,
                  resolution_code,service_id,assignment_origin,detected_at,
                  source_started_at,ack_sla_seconds,ack_sla_due_at,
                  acknowledged_at,first_investigating_at,mitigated_at,
                  resolved_at,latest_activity_at,version,group_key,member_count,
                  evidence_completeness,duplicate_of_occurrence_id,
                  primary_alertname,aggregation_rule_id,group_labels_json,
                  creation_unmapped
                ) VALUES (
                  :id,:id,1,'source-history',:title,:signal_state,'WARNING',
                  :response_state,'P2',:resolution,NULL,'UNMAPPED',:old,:old,
                  60,:old,NULL,NULL,NULL,:resolved_at,:old,1,:title,1,'COMPLETE',
                  NULL,'HistoryAlert',NULL,'{}',1
                )
                """
            ),
            [
                {
                    "id": 1,
                    "title": "resolved-old",
                    "signal_state": "RECOVERED",
                    "response_state": "RESOLVED",
                    "resolution": "FIXED",
                    "resolved_at": old,
                    "old": old,
                },
                {
                    "id": 2,
                    "title": "unresolved-old",
                    "signal_state": "FIRING",
                    "response_state": "UNACKNOWLEDGED",
                    "resolution": None,
                    "resolved_at": None,
                    "old": old,
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO incident_timeline_entry (
                  id,occurrence_id,sequence,actor_type,event_type,summary,
                  detail_json,request_id,source_ip,created_at
                ) VALUES (1,1,1,'INTERACTIVE_OPERATOR','NOTE_ADDED','old note','{}','req','local',:old)
                """
            ),
            {"old": old},
        )
        connection.execute(
            text(
                """
                INSERT INTO incident_note_content (
                  id,occurrence_id,timeline_entry_id,content_envelope,
                  redacted_at,purge_after
                ) VALUES (1,1,1,'encrypted',NULL,NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO incident_task (
                  id,occurrence_id,title,description,due_at,runbook_link,status,
                  result,version,created_at,updated_at
                ) VALUES (1,1,'done',NULL,NULL,NULL,'DONE','ok',1,:old,:old)
                """
            ),
            {"old": old},
        )

    with pytest.raises(DatabaseError, match="INCIDENT_TIMELINE_APPEND_ONLY"):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM incident_timeline_entry WHERE id=1"))

    result = persistence.cleanup_batch(as_of=now, policy=RetentionPolicy())

    assert result.deleted_by_table["incident_timeline_entry"] == 1
    assert result.deleted_by_table["operational_occurrence"] == 1
    with engine.connect() as connection:
        occurrence_rows = connection.execute(
            text("SELECT id,response_state FROM operational_occurrence ORDER BY id")
        ).all()
        incident_rows = connection.execute(
            text("SELECT id,source_state FROM incident ORDER BY id")
        ).all()
        alert_rows = connection.execute(
            text("SELECT id,source_state FROM alert ORDER BY id")
        ).all()
    assert occurrence_rows == [(2, "UNACKNOWLEDGED")]
    assert incident_rows == [(2, "FIRING")]
    assert alert_rows == [(2, "FIRING")]
