"""Million-row keyset capacity gate for append-only occurrence history."""

from __future__ import annotations

from datetime import datetime, timezone
from math import ceil
from pathlib import Path
import time

from app.adapters.persistence.incidents import SqlAlchemyIncidentStore
from app.adapters.persistence.sources import SqlAlchemySourceStore
from app.api.v1.cursor import SignedCursorCodec
from app.application.sources import EndpointDraft, SourceDraft
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


def test_million_row_history_walk_uses_bounded_keyset_pages(tmp_path: Path) -> None:
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
                SELECT value + 1 FROM sequence WHERE value < 1000000
            )
            INSERT INTO incident_occurrence (
                incident_id, occurrence_no, source_id, source_name,
                group_key, title, aggregation_rule_id, aggregation_rule_name,
                started_at, recovered_at, member_count,
                member_max_severity, handling_conclusion
            )
            SELECT
                1, value, 'src-a', 'Primary AM',
                'source=src-a|capacity', 'Capacity Incident', NULL, NULL,
                '2026-08-13 00:00:00.000000', '2026-08-13 01:00:00.000000', 1,
                'warning', 'CLOSED'
            FROM sequence
            """
        )

    store = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(
            b"incident-history-capacity-key-at-least-32-bytes"
        ),
    )
    samples: list[float] = []
    first = second = None
    for _ in range(12):
        started = time.perf_counter()
        first = store.list_occurrences(
            source_ids=("src-a",),
            conclusion=None,
            include_archived=False,
            cursor=None,
            limit=50,
        )
        second = store.list_occurrences(
            source_ids=("src-a",),
            conclusion=None,
            include_archived=False,
            cursor=first.next_cursor,
            limit=50,
        )
        samples.append(time.perf_counter() - started)

    assert first is not None and second is not None
    assert [item.occurrence_no for item in first.items] == list(
        range(1_000_000, 999_950, -1)
    )
    assert [item.occurrence_no for item in second.items] == list(
        range(999_950, 999_900, -1)
    )
    assert _p95_ms(samples) < 300
    engine.dispose()
