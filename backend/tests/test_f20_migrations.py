"""F20 stage-1 migration regressions against sanitized F17 database snapshots."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlmodel import select

import app.migrations as migration_module
from app import db as db_module
from app.db import create_sqlite_engine
from app.migrations import (
    MigrationChecksumError,
    MigrationError,
    MigrationReport,
    run_migrations,
)
from app.services.event_sources import new_event_source_id
from app.models import Incident, IncidentAudit
from app.services.lifecycle import recompute_incident_lifecycle
from app.services.notification_planner import snapshot_incidents
from app.services.retention import cleanup_expired_data


SOURCE_A = "am:safe-source-a"
SOURCE_B = "am:safe-source-b"

FROZEN_MIGRATION_CHECKSUMS = {
    1: "f005fa490d70ca9b1949e110542577d8514344c25168db7c07f2f7d35a1a452f",
    2: "0045c3f60495e6ebbeea9f22e124e1dd75af648cb655bfab5b382068bcb1f341",
    3: "b6b9562b83325f70e3316ff055de784a868714bc3c41abcb386157d0b9254350",
    4: "6a6b6e2d61db931646213e6da5f46488f8c7c39be23b9b5bcdfbfbdd17784dbd",
    5: "40577849299f55def01ad293107dc7381994b086fdce9c929f44e0a1410b9b68",
}


def _versions_from(first: int) -> list[int]:
    """Every migration at or after `first`.

    Spelled from the ledger rather than written out, so adding a migration does
    not make these tests fail for a reason that has nothing to do with them.
    """

    from app.migrations import MIGRATIONS

    return [item.version for item in MIGRATIONS if item.version >= first]


def _create_v4_schema(path: Path) -> None:
    """Create the smallest exact v4 schema needed by the F20 data migration."""

    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE schemamigration (
                    version INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    checksum VARCHAR NOT NULL,
                    applied_at DATETIME NOT NULL
                )
                """
            )
        )
        for migration in migration_module.MIGRATIONS[:4]:
            connection.execute(
                text(
                    "INSERT INTO schemamigration "
                    "(version, name, checksum, applied_at) "
                    "VALUES (:version, :name, :checksum, '2026-07-20 00:00:00')"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                },
            )
        connection.execute(
            text(
                """
                CREATE TABLE aggregationrule (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    priority INTEGER NOT NULL,
                    matchers JSON NOT NULL,
                    group_by_labels JSON NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    version INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE incident (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source_id VARCHAR NOT NULL,
                    environment VARCHAR NOT NULL,
                    group_key VARCHAR NOT NULL UNIQUE,
                    title VARCHAR NOT NULL,
                    severity VARCHAR NOT NULL,
                    source_state VARCHAR NOT NULL,
                    handling_state VARCHAR NOT NULL,
                    policy_version INTEGER NOT NULL,
                    grouping_explanation VARCHAR NOT NULL,
                    aggregation_rule_id INTEGER,
                    aggregation_rule_version INTEGER,
                    group_labels JSON NOT NULL,
                    missing_group_labels JSON NOT NULL,
                    occurrence_no INTEGER NOT NULL,
                    occurrence_started_at DATETIME NOT NULL,
                    change_version INTEGER NOT NULL,
                    change_origin VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE alert (
                    id INTEGER NOT NULL PRIMARY KEY,
                    fingerprint VARCHAR NOT NULL UNIQUE,
                    upstream_fingerprint VARCHAR NOT NULL,
                    source_id VARCHAR NOT NULL,
                    environment VARCHAR NOT NULL,
                    alertname VARCHAR NOT NULL,
                    severity VARCHAR NOT NULL,
                    cluster VARCHAR NOT NULL,
                    labels JSON NOT NULL,
                    annotations JSON NOT NULL,
                    starts_at DATETIME,
                    ends_at DATETIME,
                    source_state VARCHAR NOT NULL,
                    missing_since_at DATETIME,
                    origin VARCHAR NOT NULL,
                    evidence_completeness VARCHAR NOT NULL,
                    raw_payload JSON NOT NULL,
                    incident_id INTEGER REFERENCES incident(id),
                    first_seen_at DATETIME NOT NULL,
                    last_seen_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE incidentaudit (
                    id INTEGER NOT NULL PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incident(id),
                    actor VARCHAR NOT NULL,
                    from_state VARCHAR NOT NULL,
                    to_state VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE connectionprofile (
                    id INTEGER NOT NULL PRIMARY KEY,
                    logical_id VARCHAR NOT NULL,
                    version INTEGER NOT NULL,
                    kind VARCHAR NOT NULL,
                    name VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    base_url VARCHAR NOT NULL,
                    environment VARCHAR,
                    auth_type VARCHAR NOT NULL,
                    username VARCHAR NOT NULL,
                    secret_envelope JSON,
                    settings_json JSON NOT NULL,
                    source_id VARCHAR,
                    last_tested_at DATETIME,
                    last_test_result VARCHAR,
                    last_test_error_code VARCHAR,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    activated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationchannel (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    active_revision_id INTEGER,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    disabled_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationchannelrevision (
                    id INTEGER NOT NULL PRIMARY KEY,
                    channel_id INTEGER NOT NULL REFERENCES notificationchannel(id),
                    version INTEGER NOT NULL,
                    state VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    webhook_envelope JSON NOT NULL,
                    signing_secret_envelope JSON,
                    required_keyword VARCHAR,
                    mention_mode VARCHAR NOT NULL,
                    mention_users JSON NOT NULL,
                    mention_on JSON NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationpolicyrevision (
                    id INTEGER NOT NULL PRIMARY KEY,
                    logical_id VARCHAR NOT NULL,
                    version INTEGER NOT NULL,
                    name VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    priority INTEGER NOT NULL,
                    matchers JSON NOT NULL,
                    repeat_interval_seconds INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationroute (
                    id INTEGER NOT NULL PRIMARY KEY,
                    incident_id INTEGER NOT NULL REFERENCES incident(id),
                    occurrence_no INTEGER NOT NULL,
                    policy_revision_id INTEGER NOT NULL REFERENCES notificationpolicyrevision(id),
                    policy_name VARCHAR NOT NULL,
                    policy_version INTEGER NOT NULL,
                    policy_priority INTEGER NOT NULL,
                    match_context_json JSON NOT NULL,
                    repeat_interval_seconds INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    repeat_slot INTEGER NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationroutetarget (
                    id INTEGER NOT NULL PRIMARY KEY,
                    route_id INTEGER NOT NULL REFERENCES notificationroute(id),
                    channel_id INTEGER NOT NULL REFERENCES notificationchannel(id),
                    routed_channel_revision_id INTEGER NOT NULL REFERENCES notificationchannelrevision(id),
                    channel_name VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    channel_version INTEGER NOT NULL,
                    mention_mode VARCHAR NOT NULL,
                    mention_users JSON NOT NULL,
                    mention_on JSON NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationdelivery (
                    id INTEGER NOT NULL PRIMARY KEY,
                    event_key VARCHAR NOT NULL UNIQUE,
                    incident_id INTEGER NOT NULL REFERENCES incident(id),
                    route_id INTEGER NOT NULL REFERENCES notificationroute(id),
                    route_target_id INTEGER NOT NULL REFERENCES notificationroutetarget(id),
                    event_type VARCHAR NOT NULL,
                    incident_change_version INTEGER NOT NULL,
                    state VARCHAR NOT NULL,
                    payload_snapshot_json JSON NOT NULL,
                    scheduled_at DATETIME NOT NULL,
                    next_attempt_at DATETIME NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    next_attempt_trigger VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE notificationattempt (
                    id INTEGER NOT NULL PRIMARY KEY,
                    delivery_id INTEGER NOT NULL REFERENCES notificationdelivery(id),
                    attempt_no INTEGER NOT NULL,
                    trigger VARCHAR NOT NULL,
                    started_at DATETIME NOT NULL,
                    finished_at DATETIME,
                    outcome VARCHAR
                )
                """
            )
        )
    engine.dispose()


def _insert_profile(connection, *, profile_id: int, source_id: str, state: str) -> None:
    connection.execute(
        text(
            """
            INSERT INTO connectionprofile (
                id, logical_id, version, kind, name, state, base_url, environment,
                auth_type, username, secret_envelope, settings_json, source_id,
                last_tested_at, last_test_result, created_at, updated_at, activated_at
            ) VALUES (
                :id, 'logical-am', :version, 'ALERTMANAGER', :name, :state,
                :base_url, :environment, 'NONE', '', NULL, :settings, :source_id,
                '2026-07-20 00:00:00', 'SUCCESS', '2026-07-20 00:00:00',
                '2026-07-20 00:00:00', '2026-07-20 00:00:00'
            )
            """
        ),
        {
            "id": profile_id,
            "version": profile_id,
            "name": f"Sanitized source {profile_id}",
            "state": state,
            "base_url": f"https://source-{profile_id}.invalid",
            "environment": f"legacy-{profile_id}",
            "settings": json.dumps(
                {"poll_interval_seconds": 30, "resolution_grace_seconds": 60}
            ),
            "source_id": source_id,
        },
    )


def _insert_incident(
    connection,
    *,
    incident_id: int,
    source_id: str,
    environment: str,
    source_state: str,
    handling_state: str,
    updated_at: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO incident (
                id, source_id, environment, group_key, title, severity,
                source_state, handling_state, policy_version, grouping_explanation,
                aggregation_rule_id, aggregation_rule_version, group_labels,
                missing_group_labels, occurrence_no, occurrence_started_at,
                change_version, change_origin, created_at, updated_at
            ) VALUES (
                :id, :source_id, :environment, :group_key, 'Sanitized incident',
                'warning', :source_state, :handling_state, 1, 'legacy grouping',
                7, 1, :group_labels, '[]', :occurrence_no,
                '2026-07-20 00:00:00', :change_version, 'LIVE_POLL',
                '2026-07-20 00:00:00', :updated_at
            )
            """
        ),
        {
            "id": incident_id,
            "source_id": source_id,
            "environment": environment,
            "group_key": (
                f"source={source_id}|env={environment}|rule=7|cluster=shared"
            ),
            "source_state": source_state,
            "handling_state": handling_state,
            "group_labels": json.dumps({"cluster": "shared"}),
            "occurrence_no": 3 if incident_id == 101 else 2,
            "change_version": 8 if incident_id == 101 else 5,
            "updated_at": updated_at,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO alert (
                id, fingerprint, upstream_fingerprint, source_id, environment,
                alertname, severity, cluster, labels, annotations, source_state,
                origin, evidence_completeness, raw_payload, incident_id,
                first_seen_at, last_seen_at
            ) VALUES (
                :id, :fingerprint, :upstream_fingerprint, :source_id, :environment,
                'SanitizedAlert', 'warning', 'shared', :labels, '{}', :source_state,
                'live', 'complete', '{}', :incident_id,
                '2026-07-20 00:00:00', '2026-07-20 00:00:00'
            )
            """
        ),
        {
            "id": incident_id + 100,
            "fingerprint": f"{source_id}:fp-{incident_id}",
            "upstream_fingerprint": f"fp-{incident_id}",
            "source_id": source_id,
            "environment": environment,
            "labels": json.dumps({"cluster": "shared"}),
            "source_state": "firing" if source_state == "firing" else "resolved",
            "incident_id": incident_id,
        },
    )


@pytest.fixture
def switched_v4_database(tmp_path: Path) -> tuple[Path, object]:
    database_path = tmp_path / "sanitized-switched-v4.db"
    _create_v4_schema(database_path)
    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO aggregationrule (
                    id, name, priority, matchers, group_by_labels, enabled,
                    version, created_at, updated_at
                ) VALUES (
                    7, 'Sanitized grouping', 10, '[]', '["cluster"]', 1,
                    1, '2026-07-20 00:00:00', '2026-07-20 00:00:00'
                )
                """
            )
        )
        _insert_profile(connection, profile_id=1, source_id=SOURCE_A, state="RETIRED")
        _insert_profile(connection, profile_id=2, source_id=SOURCE_B, state="ACTIVE")
        _insert_incident(
            connection,
            incident_id=101,
            source_id=SOURCE_B,
            environment="legacy-blue",
            source_state="firing",
            handling_state="IN_PROGRESS",
            updated_at="2026-07-20 01:00:00",
        )
        _insert_incident(
            connection,
            incident_id=102,
            source_id=SOURCE_B,
            environment="legacy-green",
            source_state="recovered",
            handling_state="CLOSED",
            updated_at="2026-07-20 02:00:00",
        )
        _insert_incident(
            connection,
            incident_id=103,
            source_id="legacy",
            environment="legacy-unknown",
            source_state="recovered",
            handling_state="FALSE_POSITIVE",
            updated_at="2026-07-20 03:00:00",
        )
        connection.execute(
            text(
                """
                INSERT INTO incidentaudit (
                    id, incident_id, actor, from_state, to_state, reason, created_at
                ) VALUES (
                    1, 102, 'local-user', 'NEW', 'CLOSED', 'sanitized history',
                    '2026-07-20 02:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationchannel (
                    id, name, state, active_revision_id, created_at, updated_at
                ) VALUES (
                    1, 'Sanitized channel', 'ENABLED', 1,
                    '2026-07-20 00:00:00', '2026-07-20 00:00:00'
                );
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationchannelrevision (
                    id, channel_id, version, state, provider, webhook_envelope,
                    mention_mode, mention_users, mention_on, created_at, updated_at
                ) VALUES (
                    1, 1, 1, 'ACTIVE', 'FAKE', '{}', 'NONE', '[]', '{}',
                    '2026-07-20 00:00:00', '2026-07-20 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationpolicyrevision (
                    id, logical_id, version, name, state, priority, matchers,
                    repeat_interval_seconds, created_at, updated_at
                ) VALUES (
                    1, 'safe-policy', 1, 'Sanitized policy', 'ACTIVE', 10, '[]',
                    3600, '2026-07-20 00:00:00', '2026-07-20 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationroute (
                    id, incident_id, occurrence_no, policy_revision_id, policy_name,
                    policy_version, policy_priority, match_context_json,
                    repeat_interval_seconds, status, repeat_slot, created_at
                ) VALUES (
                    1, 102, 2, 1, 'Sanitized policy', 1, 10, '{}', 3600,
                    'RECOVERED', 0, '2026-07-20 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationroutetarget (
                    id, route_id, channel_id, routed_channel_revision_id,
                    channel_name, provider, channel_version, mention_mode,
                    mention_users, mention_on
                ) VALUES (
                    1, 1, 1, 1, 'Sanitized channel', 'FAKE', 1, 'NONE', '[]', '{}'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationdelivery (
                    id, event_key, incident_id, route_id, route_target_id,
                    event_type, incident_change_version, state,
                    payload_snapshot_json, scheduled_at, next_attempt_at,
                    attempt_count, next_attempt_trigger, created_at, updated_at
                ) VALUES (
                    1, 'safe-event-key', 102, 1, 1, 'RECOVERED', 5, 'SUCCEEDED',
                    '{}', '2026-07-20 00:00:00', '2026-07-20 00:00:00',
                    1, 'AUTO', '2026-07-20 00:00:00', '2026-07-20 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO notificationattempt (
                    id, delivery_id, attempt_no, trigger, started_at, finished_at, outcome
                ) VALUES (
                    1, 1, 1, 'AUTO', '2026-07-20 00:00:00',
                    '2026-07-20 00:00:01', 'SUCCEEDED'
                )
                """
            )
        )
    return database_path, engine


def test_new_event_source_ids_are_random_and_not_url_derived() -> None:
    first = new_event_source_id()
    second = new_event_source_id()

    assert first.startswith("src_")
    assert second.startswith("src_")
    assert first != second
    assert "http" not in first


def test_applied_migration_checksums_are_frozen() -> None:
    assert {
        migration.version: migration.checksum
        for migration in migration_module.MIGRATIONS[:5]
    } == FROZEN_MIGRATION_CHECKSUMS


def test_f20_migration_checksum_covers_schema_and_identity_helpers() -> None:
    migration = migration_module.MIGRATIONS[4]

    assert {item.__name__ for item in migration.checksum_dependencies} >= {
        "EventSource",
        "EventSourceRevision",
        "AlertmanagerEndpointRevision",
        "SourcePollRun",
        "EndpointPollResult",
        "AlertEndpointObservation",
        "MonitoredCluster",
        "AggregationRuleSource",
        "NotificationPolicySource",
        "HistoricalDataSource",
        "SourceEvidenceBinding",
        "build_group_key_v2",
        "canonical_alertmanager_url",
        "source_id_for_alertmanager",
    }


def test_single_source_v4_snapshot_maps_current_profile_to_enabled(tmp_path: Path) -> None:
    database_path = tmp_path / "sanitized-single-v4.db"
    _create_v4_schema(database_path)
    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        _insert_profile(connection, profile_id=1, source_id=SOURCE_A, state="ACTIVE")

    report = run_migrations(engine, database_path=database_path)

    assert report.applied_versions == _versions_from(5)
    assert report.details[5] == {
        "archived_source_count": 0,
        "collision_count": 0,
        "enabled_source_count": 1,
        "incident_count": 0,
        "source_count": 1,
        "superseded_incident_count": 0,
    }
    with engine.connect() as connection:
        source = connection.execute(
            text(
                "SELECT id, lifecycle_state, active_revision_id "
                "FROM eventsource"
            )
        ).one()
        revision_count = connection.execute(
            text("SELECT COUNT(*) FROM eventsourcerevision")
        ).scalar_one()
        endpoint_count = connection.execute(
            text("SELECT COUNT(*) FROM alertmanagerendpointrevision")
        ).scalar_one()
    assert source[0] == SOURCE_A
    assert source[1] == "ENABLED"
    assert source[2] is not None
    assert revision_count == endpoint_count == 1


def test_switched_sources_collision_and_f17_history_migrate_without_planning(
    switched_v4_database,
) -> None:
    database_path, engine = switched_v4_database

    first = run_migrations(engine, database_path=database_path)
    second = run_migrations(engine, database_path=database_path)

    assert first.applied_versions == _versions_from(5)
    assert first.backup_path is not None and first.backup_path.exists()
    assert first.backup_path.name.startswith(f"{database_path.name}.pre-f20-")
    assert second.applied_versions == []
    assert second.backup_path == first.backup_path
    assert set(first.details[5]) == {
        "archived_source_count",
        "collision_count",
        "enabled_source_count",
        "incident_count",
        "source_count",
        "superseded_incident_count",
    }
    assert ".invalid" not in repr(first.details)

    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        sources = connection.execute(
            text(
                "SELECT id, lifecycle_state, active_revision_id "
                "FROM eventsource ORDER BY id"
            )
        ).all()
        incidents = connection.execute(
            text(
                "SELECT id, group_key, legacy_group_key, group_key_version, "
                "superseded_by_incident_id, handling_state, occurrence_no, "
                "change_version, change_origin FROM incident ORDER BY id"
            )
        ).all()
        alert_owners = connection.execute(
            text(
                "SELECT id, incident_id FROM alert "
                "WHERE source_id = :source_id ORDER BY id"
            ),
            {"source_id": SOURCE_B},
        ).all()
        preserved = {
            "audits": connection.execute(
                text("SELECT COUNT(*) FROM incidentaudit WHERE incident_id = 102")
            ).scalar_one(),
            "routes": connection.execute(
                text("SELECT COUNT(*) FROM notificationroute WHERE incident_id = 102")
            ).scalar_one(),
            "deliveries": connection.execute(
                text("SELECT COUNT(*) FROM notificationdelivery WHERE incident_id = 102")
            ).scalar_one(),
            "attempts": connection.execute(
                text("SELECT COUNT(*) FROM notificationattempt")
            ).scalar_one(),
        }
        migration_count = connection.execute(
            text("SELECT COUNT(*) FROM schemamigration WHERE version = 5")
        ).scalar_one()
        scope_modes = (
            connection.execute(
                text("SELECT DISTINCT scope_mode FROM aggregationrule")
            ).scalar_one(),
            connection.execute(
                text("SELECT DISTINCT scope_mode FROM notificationpolicyrevision")
            ).scalar_one(),
        )
        pragmas = (
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
            str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()).lower(),
            connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one(),
        )

    assert {
        "eventsource",
        "eventsourcerevision",
        "alertmanagerendpointrevision",
        "sourcepollrun",
        "endpointpollresult",
        "alertendpointobservation",
        "monitoredcluster",
        "aggregationrulesource",
        "notificationpolicysource",
        "historicaldatasource",
        "sourceevidencebinding",
    } <= tables
    assert pragmas == (1, "wal", 5000)
    assert scope_modes == ("ALL", "ALL")

    by_source = {row[0]: row for row in sources}
    assert by_source[SOURCE_A][1:] == ("ARCHIVED", None)
    assert by_source[SOURCE_B][1] == "ENABLED"
    assert by_source[SOURCE_B][2] is not None
    assert by_source["legacy"][1:] == ("ARCHIVED", None)

    by_incident = {row[0]: row for row in incidents}
    canonical = by_incident[101]
    superseded = by_incident[102]
    assert canonical[1] == "source=am%3Asafe-source-b|rule=7|cluster=shared"
    assert "env=" not in canonical[1]
    assert canonical[2].startswith(f"source={SOURCE_B}|env=legacy-blue")
    assert canonical[3:9] == (2, None, "IN_PROGRESS", 3, 8, "MIGRATION")
    assert superseded[2].startswith(f"source={SOURCE_B}|env=legacy-green")
    assert superseded[3] == 1
    assert superseded[4] == 101
    assert superseded[5:9] == ("CLOSED", 2, 5, "MIGRATION")
    assert {row[1] for row in alert_owners} == {101}
    assert preserved == {"audits": 1, "routes": 1, "deliveries": 1, "attempts": 1}
    assert migration_count == 1


def test_f20_checksum_mismatch_fails_closed(switched_v4_database) -> None:
    database_path, engine = switched_v4_database
    run_migrations(engine, database_path=database_path)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE schemamigration SET checksum = 'tampered' WHERE version = 5")
        )

    with pytest.raises(MigrationChecksumError, match="checksum"):
        run_migrations(engine, database_path=database_path)


def test_f20_failure_rolls_back_schema_data_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "sanitized-failure-v4.db"
    _create_v4_schema(database_path)
    engine = create_sqlite_engine(database_path)
    with engine.begin() as connection:
        _insert_profile(connection, profile_id=1, source_id=SOURCE_A, state="ACTIVE")

    migration = migration_module.MIGRATIONS[4]

    def fail_after_apply(connection):
        migration.apply(connection)
        raise RuntimeError("injected F20 migration failure")

    monkeypatch.setattr(
        migration_module,
        "MIGRATIONS",
        (*migration_module.MIGRATIONS[:4], replace(migration, apply=fail_after_apply)),
    )

    with pytest.raises(MigrationError, match="migration 5"):
        run_migrations(engine, database_path=database_path)

    assert "eventsource" not in inspect(engine).get_table_names()
    assert "group_key_version" not in {
        column["name"] for column in inspect(engine).get_columns("incident")
    }
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM schemamigration")
        ).scalar_one() == 4
    assert list(tmp_path.glob("sanitized-failure-v4.db.pre-f20-*.bak"))


def test_upgrade_startup_does_not_enter_regroup_or_planner_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_sqlite_engine(tmp_path / "startup-boundary.db")
    monkeypatch.setattr(db_module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        db_module,
        "run_migrations",
        lambda *args, **kwargs: MigrationReport([5], None),
    )

    from app.services import aggregation_rules

    def fail_regroup(*args, **kwargs):
        raise AssertionError("F20 upgrade must not enter regroup/Planner paths")

    monkeypatch.setattr(
        aggregation_rules, "regroup_all_with_aggregation_rules", fail_regroup
    )

    db_module.init_db()


def test_superseded_history_is_excluded_from_current_lifecycle(session) -> None:
    canonical = Incident(
        group_key="source=safe|rule=1|cluster=canonical",
        source_id="safe",
        source_state="firing",
    )
    session.add(canonical)
    session.flush()
    superseded = Incident(
        group_key="superseded=history",
        source_id="safe",
        source_state="firing",
        superseded_by_incident_id=canonical.id,
    )
    session.add(superseded)
    session.flush()
    session.add(
        IncidentAudit(
            incident_id=canonical.id,
            from_state="NEW",
            to_state="IN_PROGRESS",
            reason="preserve canonical",
        )
    )
    session.commit()

    snapshots = snapshot_incidents(session, source_id="safe")
    recompute_incident_lifecycle(
        session,
        datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
        source_id="safe",
    )
    session.flush()

    assert set(snapshots) == {canonical.id}
    stored = session.exec(
        select(Incident).where(Incident.id == superseded.id)
    ).one()
    assert stored.source_state == "firing"

    cleanup_expired_data(
        session,
        datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc),
        retention_days=30,
    )
    assert session.get(Incident, superseded.id) is not None
