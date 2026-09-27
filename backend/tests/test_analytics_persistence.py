"""Golden-dataset acceptance for the deterministic Analytics read model."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from app.adapters.persistence.analytics import SqlAlchemyAnalyticsStore
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _store(tmp_path: Path) -> tuple[SqlAlchemyAnalyticsStore, Path]:
    path = tmp_path / "analytics.db"
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=path))
    upgrade_database(engine)
    return SqlAlchemyAnalyticsStore(create_session_factory(engine)), path


def _insert_occurrence(
    path: Path,
    *,
    occurrence_id: int,
    incident_id: int,
    source_id: str,
    service_id: int | None,
    severity: str,
    detected_at: str,
    investigating_at: str | None,
    resolved_at: str | None,
    acknowledged_at: str | None,
    ack_due_at: str,
    member_count: int,
    creation_unmapped: int | None,
) -> None:
    # Unit data is inserted through sqlite3 so this test can focus on the
    # rebuildable projection without constructing unrelated alerting parents.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO operational_occurrence (
              id, incident_id, occurrence_no, source_id, group_key, title,
              signal_state, signal_severity, response_state, response_priority,
              resolution_code, duplicate_of_occurrence_id, service_id,
              primary_alertname, aggregation_rule_id, group_labels_json,
              assignment_origin, member_count, evidence_completeness,
              detected_at, source_started_at, ack_sla_seconds, ack_sla_due_at,
              acknowledged_at, first_investigating_at, mitigated_at, resolved_at,
              latest_activity_at, version, creation_unmapped
            ) VALUES (
              ?, ?, 1, ?, 'group', 'title', 'FIRING', ?, ?, 'P3', ?, NULL, ?,
              'HighErrorRate', NULL, '{}', ?, ?, 'COMPLETE', ?, ?, 900, ?, ?, ?,
              NULL, ?, ?, 1, ?
            )
            """,
            (
                occurrence_id,
                incident_id,
                source_id,
                severity,
                "RESOLVED" if resolved_at else "UNACKNOWLEDGED",
                "FIXED" if resolved_at else None,
                service_id,
                "UNMAPPED" if service_id is None else "MAPPING",
                member_count,
                detected_at,
                detected_at,
                ack_due_at,
                acknowledged_at,
                investigating_at,
                resolved_at,
                resolved_at or detected_at,
                creation_unmapped,
            ),
        )


def test_empty_read_model_is_pending_not_zero(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)

    overview = store.overview(range_value="7d", now=datetime(2026, 9, 8, 10, 47, tzinfo=UTC))

    assert overview.freshness == "ROLLUP_PENDING"
    assert overview.signal.new_occurrences == 0


def test_refresh_builds_exact_formulas_and_unfinished_ages(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    _insert_occurrence(
        path,
        occurrence_id=1,
        incident_id=101,
        source_id="source-a",
        service_id=7,
        severity="critical",
        detected_at="2026-09-08 08:00:00",
        investigating_at="2026-09-08 08:05:00",
        resolved_at="2026-09-08 08:30:00",
        acknowledged_at="2026-09-08 08:05:00",
        ack_due_at="2026-09-08 08:15:00",
        member_count=4,
        creation_unmapped=0,
    )
    _insert_occurrence(
        path,
        occurrence_id=2,
        incident_id=102,
        source_id="source-a",
        service_id=7,
        severity="critical",
        detected_at="2026-09-08 09:00:00",
        investigating_at=None,
        resolved_at=None,
        acknowledged_at=None,
        ack_due_at="2026-09-08 09:10:00",
        member_count=2,
        creation_unmapped=None,
    )
    now = datetime(2026, 9, 8, 10, 47, tzinfo=UTC)

    result = store.refresh(now=now)
    overview = store.overview(
        range_value="24h",
        source_id="source-a",
        service_id=7,
        signal_severity="critical",
        now=now,
    )

    assert result.bucket_count == 1
    assert overview.freshness == "READY"
    assert overview.window.end.isoformat() == "2026-09-08T10:00:00+00:00"
    assert overview.signal.new_occurrences == 2
    assert overview.signal.new_alert_instances == 6
    assert overview.signal.compression.numerator == 4
    assert overview.signal.compression.denominator == 6
    assert overview.signal.alerts_per_occurrence.ratio == 3.0
    assert overview.signal.creation_unmapped.denominator == 1
    assert overview.signal.creation_assignment_unknown == 1
    assert overview.signal.ack_sla_breach.numerator == 1
    assert overview.signal.ack_sla_breach.denominator == 2
    assert overview.response.mtta.completed.count == 1
    assert overview.response.mtta.completed.median_ms == 300_000
    assert overview.response.mtta.unfinished.count == 1
    assert overview.response.mtta.unfinished.median_ms == 3_600_000
    assert overview.response.resolution.completed.median_ms == 1_800_000
    assert overview.response.resolution.unfinished.median_ms == 3_600_000


def test_filters_are_exact_and_do_not_trigger_a_refresh(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    _insert_occurrence(
        path,
        occurrence_id=3,
        incident_id=103,
        source_id="source-b",
        service_id=None,
        severity="warning",
        detected_at="2026-09-08 09:00:00",
        investigating_at=None,
        resolved_at=None,
        acknowledged_at=None,
        ack_due_at="2026-09-08 09:10:00",
        member_count=1,
        creation_unmapped=1,
    )
    now = datetime(2026, 9, 8, 10, 47, tzinfo=UTC)
    store.refresh(now=now)
    before = path.stat().st_mtime_ns

    selected = store.overview(
        range_value="24h",
        source_id="source-b",
        service_id=None,
        signal_severity="warning",
        now=now,
    )
    absent = store.overview(
        range_value="24h",
        source_id="source-a",
        now=now,
    )

    assert selected.signal.new_occurrences == 1
    assert selected.signal.creation_unmapped.ratio == 1.0
    assert selected.signal.current_unmapped == 1
    assert absent.signal.new_occurrences == 0
    assert path.stat().st_mtime_ns == before


def test_notification_and_ai_rates_use_logical_facts_and_execution_modes(
    tmp_path: Path,
) -> None:
    store, path = _store(tmp_path)
    _insert_occurrence(
        path,
        occurrence_id=4,
        incident_id=104,
        source_id="source-a",
        service_id=7,
        severity="warning",
        detected_at="2026-09-08 08:00:00",
        investigating_at="2026-09-08 08:05:00",
        resolved_at=None,
        acknowledged_at="2026-09-08 08:05:00",
        ack_due_at="2026-09-08 08:15:00",
        member_count=3,
        creation_unmapped=0,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for delivery_id, state, mode, succeeded_at, suppression in (
            (1, "SUCCEEDED", "FAKE", "2026-09-08 08:00:02", None),
            (2, "PERMANENTLY_FAILED", "FAKE", None, None),
            (3, "PENDING", "EXTERNAL", None, None),
            (4, "SUPPRESSED", "EXTERNAL", None, "STORM_AGGREGATED"),
        ):
            connection.execute(
                """
                INSERT INTO notification_delivery (
                  id, event_key, incident_id, occurrence_no, route_id,
                  route_target_id, event_type, incident_change_version,
                  repeat_slot, state, payload_snapshot_json, scheduled_at,
                  next_attempt_at, attempt_count, next_attempt_trigger,
                  lease_token, lease_expires_at, suppression_reason, created_at,
                  updated_at, succeeded_at, execution_mode
                ) VALUES (
                  ?, ?, 104, 1, 1, 1, 'FIRING_OPENED', 1, NULL, ?, '{}',
                  '2026-09-08 08:00:00', '2026-09-08 08:00:00', 0, 'AUTO',
                  NULL, NULL, ?, '2026-09-08 08:00:00',
                  '2026-09-08 08:00:00', ?, ?
                )
                """,
                (delivery_id, f"event-{delivery_id}", state, suppression, succeeded_at, mode),
            )
        for run_id, status, mode, requests, safe_code in (
            ("run-p2", "COMPLETED", "FAKE", 1, None),
            ("run-contract", "FAILED", "EXTERNAL", 1, "MODEL_CONTRACT_INVALID"),
            ("run-dependency", "FAILED", "UNKNOWN_LEGACY", 0, "MODEL_UNAVAILABLE"),
        ):
            connection.execute(
                """
                INSERT INTO investigation_run_v2 (
                  id, occurrence_id, request_key, provider_profile_id,
                  model_channel_id, model_revision, status, job_id, request_id,
                  source_ip, request_count, tool_call_count, input_tokens,
                  output_tokens, safe_error_code, created_at, updated_at,
                  model_execution_mode
                ) VALUES (
                  ?, 4, ?, NULL, NULL, NULL, ?, NULL, 'request', 'local-test',
                  ?, 0, 0, 0, ?, '2026-09-08 08:00:00',
                  '2026-09-08 08:00:00', ?
                )
                """,
                (run_id, run_id, status, requests, safe_code, mode),
            )
        connection.execute(
            """
            INSERT INTO evidence_snapshot_v2 (
              investigation_id, schema_revision, alert_evidence_json,
              metric_evidence_json, degraded_domains_json, content_hash, created_at
            ) VALUES ('run-p2', 2, '[]', '[]', '[]', 'hash', '2026-09-08 08:00:01')
            """
        )
        connection.execute(
            """
            INSERT INTO investigation_activity_v2 (
              investigation_id, sequence, kind, status, safe_code,
              evidence_ids_json, created_at
            ) VALUES (
              'run-p2', 1, 'tool_call_completed', 'COMPLETED', 'OK', '[]',
              '2026-09-08 08:00:02'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO investigation_report_v2 (
              investigation_id, schema_revision, report_json, created_at
            ) VALUES (
              'run-p2', 2, '{"recommended_actions":[{"title_zh":"人工核对"}]}',
              '2026-09-08 08:00:03'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO investigation_feedback_v2 (
              investigation_id, sequence, rating, created_at
            ) VALUES ('run-p2', 1, 'ADOPTED', '2026-09-08 08:05:00')
            """
        )
        for fact_id, kind in ((1, "FLAPPING"), (2, "STORM")):
            connection.execute(
                """
                INSERT INTO noise_lifecycle_fact (
                  id, fact_key, kind, transition, subject_key, source_id,
                  service_key, signal_severity, occurred_at
                ) VALUES (?, ?, ?, 'ACTIVATED', ?, 'source-a', '7', 'warning',
                          '2026-09-08 08:10:00')
                """,
                (fact_id, f"fact-{fact_id}", kind, f"subject-{fact_id}"),
            )
    now = datetime(2026, 9, 8, 10, 47, tzinfo=UTC)

    store.refresh(now=now)
    overview = store.overview(range_value="24h", now=now)

    assert overview.notifications["FAKE"].succeeded == 1
    assert overview.notifications["FAKE"].permanently_failed == 1
    assert overview.notifications["FAKE"].success_rate.ratio == 0.5
    assert overview.notifications["FAKE"].latency.median_ms == 2_000
    assert overview.notifications["EXTERNAL"].pending_backlog == 1
    assert overview.notifications["EXTERNAL"].noise == 1
    assert overview.ai["FAKE"].p2_valid == 1
    assert overview.ai["FAKE"].p2_success.ratio == 1.0
    assert overview.ai["FAKE"].feedback_response.ratio == 1.0
    assert overview.ai["FAKE"].feedback_adoption.ratio == 1.0
    assert overview.ai["FAKE"].p0_mtti.median_ms == 1_000
    assert overview.ai["FAKE"].p1_mtti.median_ms == 2_000
    assert overview.ai["FAKE"].p2_mtti.median_ms == 3_000
    assert overview.ai["EXTERNAL"].contract_rejected == 1
    assert overview.ai["EXTERNAL"].p2_success.ratio == 0.0
    assert overview.ai["UNKNOWN_LEGACY"].dependency_failed == 1
    assert overview.ai["UNKNOWN_LEGACY"].p2_success.ratio is None
    assert overview.signal.flapping_activations == 1
    assert overview.signal.storm_activations == 1


def test_projection_retention_is_bounded_and_reentrant(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    with sqlite3.connect(path) as connection:
        for day in ("2025-09-08", "2025-09-09"):
            connection.execute(
                """
                INSERT INTO analytics_daily_bucket (
                  day_utc, source_id, service_key, signal_severity,
                  hours_json, updated_at
                ) VALUES (?, 'source-a', '7', 'warning', '{}', '2026-09-09')
                """,
                (day,),
            )
            connection.execute(
                """
                INSERT INTO analytics_duration_sample (
                  fact_key, day_utc, source_id, service_key, signal_severity,
                  metric, value_ms, occurred_at
                ) VALUES (?, ?, 'source-a', '7', 'warning', 'mtta_completed', 1, ?)
                """,
                (f"sample-{day}", day, f"{day} 00:00:00"),
            )

    first = store.cleanup(now=datetime(2026, 9, 9, tzinfo=UTC), retention_days=365)
    second = store.cleanup(now=datetime(2026, 9, 9, tzinfo=UTC), retention_days=365)

    assert first == (1, 1)
    assert second == (0, 0)


def test_overview_stays_bounded_with_one_million_unrelated_business_rows(
    tmp_path: Path,
) -> None:
    store, path = _store(tmp_path)
    store.refresh(now=datetime(2026, 9, 9, 12, tzinfo=UTC))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            WITH RECURSIVE rows(value) AS (
              SELECT 1 UNION ALL SELECT value + 1 FROM rows WHERE value < 1000000
            )
            INSERT INTO platform_event (
              sequence, event_type, subject_type, subject_id, created_at
            )
            SELECT value, 'capacity.fixture', 'history', CAST(value AS TEXT),
                   '2025-01-01 00:00:00'
            FROM rows
            """
        )
    started = time.perf_counter()

    overview = store.overview(
        range_value="7d", now=datetime(2026, 9, 9, 12, tzinfo=UTC)
    )

    elapsed = time.perf_counter() - started
    assert overview.freshness == "READY"
    assert elapsed < 0.3
