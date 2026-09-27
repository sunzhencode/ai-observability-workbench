"""Local capacity gates for the current Incident queue and operator writes."""

from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from pathlib import Path
import time

from app.adapters.persistence.incidents import SqlAlchemyIncidentStore
from app.adapters.persistence.sources import SqlAlchemySourceStore
from app.application.sources import EndpointDraft, SourceDraft
from app.platform.cursor import SignedCursorCodec
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _p95_ms(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[ceil(len(ordered) * 0.95) - 1] * 1000


def test_ten_thousand_active_alerts_keep_queue_and_writes_bounded(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "incident-operations.db")
    )
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    sources = SqlAlchemySourceStore(sessions)
    sources.create_source(
        SourceDraft(
            "src-a",
            "Primary AM",
            (EndpointDraft(0, "https://am.invalid"),),
        ),
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            WITH RECURSIVE sequence(value) AS (
                VALUES (1)
                UNION ALL
                SELECT value + 1 FROM sequence WHERE value < 10000
            )
            INSERT INTO incident (
                id, source_id, group_key, title, severity, source_state,
                freshness_state, handling_state, handling_version,
                aggregation_rule_id, aggregation_rule_version,
                group_labels_json, missing_labels_json, occurrence_no,
                occurrence_started_at, change_version, change_origin, updated_at
            )
            SELECT
                value, 'src-a', 'source=src-a|capacity-' || value,
                'Capacity Incident ' || value, 'warning', 'FIRING',
                'CURRENT', 'NEW', 1, NULL, NULL, '{}', '[]', 1,
                '2026-08-13 00:00:00.000000', 1, 'LIVE_POLL',
                '2026-08-13 00:00:00.000000'
            FROM sequence
            """
        )
        connection.exec_driver_sql(
            """
            WITH RECURSIVE sequence(value) AS (
                VALUES (1)
                UNION ALL
                SELECT value + 1 FROM sequence WHERE value < 10000
            )
            INSERT INTO alert (
                id, source_id, upstream_fingerprint, alertname, severity,
                cluster, labels_json, annotations_json, raw_json, origin,
                evidence_completeness, source_state, missing_since_at,
                starts_at, last_seen_at, endpoint_positions_json, incident_id
            )
            SELECT
                value, 'src-a', 'fingerprint-' || value, 'CapacityAlert',
                'warning', 'cluster-a', '{}', '{}', '{}', 'LIVE_POLL',
                'COMPLETE', 'FIRING', NULL, '2026-08-13 00:00:00.000000',
                '2026-08-13 00:00:00.000000', '[0]', value
            FROM sequence
            """
        )

    queue_samples: list[float] = []
    # Twenty observations keep p95 from degenerating into "the single maximum"
    # (ceil(12 * .95) == 12), while still enforcing the same 300 ms contract.
    for _ in range(20):
        started = time.perf_counter()
        incidents = sources.list_incidents(source_id="src-a")
        queue_samples.append(time.perf_counter() - started)
    assert len(incidents) == 10_000
    assert _p95_ms(queue_samples) < 300

    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            WITH RECURSIVE sequence(value) AS (
                VALUES (1)
                UNION ALL
                SELECT value + 1 FROM sequence WHERE value < 10000
            )
            INSERT INTO operational_occurrence (
                id, incident_id, occurrence_no, source_id, group_key, title,
                signal_state, signal_severity, response_state, response_priority,
                resolution_code, service_id, assignment_origin, member_count,
                evidence_completeness, detected_at, source_started_at,
                ack_sla_seconds, ack_sla_due_at, acknowledged_at,
                first_investigating_at, mitigated_at, resolved_at,
                latest_activity_at, version
            )
            SELECT
                value, value, 1, 'src-a', 'source=src-a|capacity-' || value,
                'Capacity Incident ' || value, 'FIRING', 'warning',
                'UNACKNOWLEDGED', 'P2', NULL, NULL, 'UNMAPPED', 1, 'COMPLETE',
                '2026-08-13 00:00:00.000000', '2026-08-13 00:00:00.000000',
                900, '2026-08-13 00:15:00.000000', NULL, NULL, NULL, NULL,
                '2026-08-13 00:00:00.000000', 1
            FROM sequence
            """
        )

    incident_store = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(
            b"incident-capacity-cursor-key-at-least-32-bytes"
        ),
    )
    operational_samples: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        page = incident_store.list_operational_occurrences(
            view="ALL",
            source_ids=("src-a",),
            signal_states=(),
            cursor=None,
            limit=50,
            now=datetime(2026, 8, 13, 1, tzinfo=UTC),
        )
        operational_samples.append(time.perf_counter() - started)
    assert len(page.items) == 50
    assert page.next_cursor is not None
    assert _p95_ms(operational_samples) < 300

    write_samples: list[float] = []
    for incident_id in range(1, 13):
        started = time.perf_counter()
        incident_store.change_handling(
            incident_id,
            target="IN_PROGRESS",
            reason="capacity gate",
            actor="capacity-test",
            expected_version=1,
            now=datetime(2026, 8, 13, 1, incident_id, tzinfo=UTC),
        )
        write_samples.append(time.perf_counter() - started)
    assert _p95_ms(write_samples) < 500
    engine.dispose()
