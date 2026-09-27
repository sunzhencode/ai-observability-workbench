from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from app.platform.persistence.codecs import (
    aware_utc,
    canonical_json,
    stored_utc,
    stored_utc_text,
    stringified_json,
    unicode_json,
)


def test_sqlite_timestamp_round_trip_uses_naive_storage_and_aware_utc_reads() -> None:
    source = datetime(2026, 9, 11, 20, 30, tzinfo=timezone(timedelta(hours=8)))

    stored = stored_utc(source)

    assert stored == datetime(2026, 9, 11, 12, 30)
    assert stored.tzinfo is None
    assert aware_utc(stored) == datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    assert aware_utc(None) is None
    assert stored_utc_text(source) == "2026-09-11 12:30:00"


def test_json_codecs_keep_each_existing_persistence_contract_explicit() -> None:
    payload = {"snow": "雪", "order": 2}

    assert canonical_json(payload) == '{"order":2,"snow":"\\u96ea"}'
    assert unicode_json(payload) == '{"order":2,"snow":"雪"}'
    assert stringified_json({"timestamp": datetime(2026, 9, 11)}) == (
        '{"timestamp":"2026-09-11 00:00:00"}'
    )
