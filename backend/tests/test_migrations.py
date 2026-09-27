"""Versioned SQLite migration and connection-baseline tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.db import create_sqlite_engine
from app.migrations import MigrationChecksumError, run_migrations


def _legacy_database(path: Path) -> None:
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE alert (
                    id INTEGER NOT NULL PRIMARY KEY,
                    fingerprint VARCHAR NOT NULL,
                    source_state VARCHAR NOT NULL DEFAULT 'firing',
                    starts_at DATETIME,
                    first_seen_at DATETIME,
                    incident_id INTEGER
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE incident (
                    id INTEGER NOT NULL PRIMARY KEY,
                    group_key VARCHAR NOT NULL,
                    source_state VARCHAR NOT NULL DEFAULT 'firing',
                    handling_state VARCHAR NOT NULL DEFAULT 'NEW',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO incident (
                    id, group_key, source_state, handling_state, created_at, updated_at
                ) VALUES (
                    1, 'legacy-group', 'firing', 'CLOSED',
                    '2026-07-18 01:00:00', '2026-07-18 02:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO alert (
                    id, fingerprint, source_state, starts_at, first_seen_at, incident_id
                ) VALUES (
                    1, 'legacy-fp', 'firing', '2026-07-18 00:30:00',
                    '2026-07-18 00:45:00', 1
                )
                """
            )
        )
    engine.dispose()


def test_old_database_is_backed_up_migrated_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    _legacy_database(database_path)
    engine = create_sqlite_engine(database_path)

    first = run_migrations(engine, database_path=database_path)

    assert first.applied_versions
    assert first.backup_path is not None
    assert first.backup_path.exists()
    assert first.backup_path.stat().st_size > 0
    with sqlite3.connect(first.backup_path) as backup:
        legacy_columns = {
            str(row[1]) for row in backup.execute("PRAGMA table_info(incident)")
        }
    assert "occurrence_no" not in legacy_columns
    inspector = inspect(engine)
    incident_columns = {item["name"] for item in inspector.get_columns("incident")}
    assert {
        "aggregation_rule_id",
        "aggregation_rule_version",
        "group_labels",
        "missing_group_labels",
        "occurrence_no",
        "occurrence_started_at",
        "change_version",
        "change_origin",
    } <= incident_columns
    assert {
        "schemamigration",
        "connectionprofile",
        "notificationchannel",
        "notificationchannelrevision",
        "notificationpolicyrevision",
        "notificationroute",
        "notificationdelivery",
        "notificationattempt",
        "configaudit",
    } <= set(inspector.get_table_names())

    with engine.connect() as connection:
        occurrence = connection.execute(
            text(
                "SELECT occurrence_no, occurrence_started_at, change_origin "
                "FROM incident WHERE id = 1"
            )
        ).one()
        migration_count = connection.execute(
            text("SELECT COUNT(*) FROM schemamigration")
        ).scalar_one()
    assert occurrence[0] == 1
    assert str(occurrence[1]).startswith("2026-07-18 00:30:00")
    assert occurrence[2] == "MIGRATION"

    second = run_migrations(engine, database_path=database_path)
    assert second.applied_versions == []
    assert second.backup_path == first.backup_path
    assert len(list(tmp_path.glob("legacy.db.pre-f17-*.bak"))) == 1
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM schemamigration")
        ).scalar_one() == migration_count


def test_migration_checksum_drift_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "checksum.db"
    _legacy_database(database_path)
    engine = create_sqlite_engine(database_path)
    run_migrations(engine, database_path=database_path)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE schemamigration SET checksum = 'tampered' WHERE version = 1")
        )

    with pytest.raises(MigrationChecksumError, match="checksum"):
        run_migrations(engine, database_path=database_path)


def test_sqlite_connection_pragmas_are_applied(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "pragmas.db")
    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
