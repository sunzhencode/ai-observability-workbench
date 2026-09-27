"""F21 migration v6: adopt existing config so removing legacy leaves no orphans.

The shape that matters is the reporter's real database: two legacy source_ids
carrying every Alert and Incident, two ARCHIVED placeholder EventSource rows
from migration 5, and a working Alertmanager that only ever existed in `.env`.
Removing the legacy poll path without adoption would strand all of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db import create_sqlite_engine
from app.migrations import run_migrations
from test_f20_migrations import (  # noqa: E402 - shared v4 schema builder
    _create_v4_schema,
    _insert_incident,
    _insert_profile,
)

LEGACY_A = "am:a17d2cdb8c0b8b26"
LEGACY_B = "legacy"
ENV_URL = "http://alertmanager.test"
ENV_THANOS = "http://thanos.test"

# migration 5 also leaves a RETIRED revision/endpoint for the archived
# placeholder, so assertions must scope to the source v6 actually adopted.
ADOPTED_ENDPOINT_SQL = (
    "SELECT e.canonical_url, e.auth_kind, e.secret_envelope_json "
    "FROM alertmanagerendpointrevision e "
    "JOIN eventsourcerevision r ON r.id = e.source_revision_id "
    "JOIN eventsource s ON s.id = r.source_id "
    "WHERE s.lifecycle_state != 'ARCHIVED'"
)


@pytest.fixture(autouse=True)
def env_sources(monkeypatch):
    """The reporter's situation: addresses in `.env`, no active profile."""
    from app.config import settings

    monkeypatch.setattr(settings, "alertmanager_url", ENV_URL)
    monkeypatch.setattr(settings, "alertmanager_token", "")
    monkeypatch.setattr(settings, "thanos_url", ENV_THANOS)
    monkeypatch.setattr(settings, "thanos_token", "")


def build_reporter_shape(path: Path):
    """A v4 database whose data sits on two legacy source ids."""
    _create_v4_schema(path)
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        # A real v4+ database has configaudit (migration 2 creates it); the
        # synthetic schema omits tables it does not otherwise need.
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS configaudit ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type VARCHAR NOT NULL, "
                "resource_id VARCHAR NOT NULL, action VARCHAR NOT NULL, "
                "actor VARCHAR NOT NULL, redacted_diff_json JSON NOT NULL, "
                "result VARCHAR NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        # RETIRED profiles: migration 5 archives them instead of enabling.
        _insert_profile(connection, profile_id=1, source_id=LEGACY_A, state="RETIRED")
        for incident_id, source_id in ((101, LEGACY_A), (102, LEGACY_B)):
            _insert_incident(
                connection,
                incident_id=incident_id,
                source_id=source_id,
                environment="prod",
                source_state="firing",
                handling_state="NEW",
                updated_at="2026-07-20 00:00:00",
            )
    return engine


def rows(engine, sql: str, params: dict | None = None) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params or {}).all()]


def test_adoption_reproduces_and_repairs_the_reporter_shape(tmp_path: Path) -> None:
    engine = build_reporter_shape(tmp_path / "reporter.db")

    report = run_migrations(engine, database_path=tmp_path / "reporter.db")

    assert 6 in report.applied_versions
    details = report.details[6]
    assert details["adopted_alertmanager"] == 1
    assert details["adopted_thanos"] == 1
    # Two legacy ids across two tables = four repoint statements.
    assert details["repointed_source_ids"] == 4
    assert details["repointed_rows"] == 4

    # Every row now points at one managed, non-archived source.
    orphans = rows(
        engine,
        "SELECT source_id, count(*) FROM alert WHERE source_id NOT IN "
        "(SELECT id FROM eventsource WHERE lifecycle_state != 'ARCHIVED') "
        "GROUP BY source_id",
    )
    assert orphans == []
    assert rows(engine, "SELECT DISTINCT source_id FROM incident") == rows(
        engine, "SELECT DISTINCT source_id FROM alert"
    )


def test_adopted_source_lands_disabled_and_untested(tmp_path: Path) -> None:
    """A stale address must not silently start polling after an upgrade.

    F22 collapsed the two-revision model, so what keeps the adopted address
    out of the poll loop is its DISABLED lifecycle -- not the absence of an
    active configuration row.  The configuration exists and is still untested.
    """
    engine = build_reporter_shape(tmp_path / "disabled.db")
    run_migrations(engine, database_path=tmp_path / "disabled.db")

    managed = rows(
        engine,
        "SELECT lifecycle_state FROM eventsource "
        "WHERE lifecycle_state != 'ARCHIVED'",
    )
    assert managed == [("DISABLED",)]

    revision = rows(
        engine,
        "SELECT r.internal_state, r.last_test_status FROM eventsourcerevision r "
        "JOIN eventsource s ON s.id = r.source_id "
        "WHERE s.lifecycle_state != 'ARCHIVED'",
    )
    assert revision == [("ACTIVE", None)]


def test_adopted_alertmanager_carries_the_env_address(tmp_path: Path) -> None:
    engine = build_reporter_shape(tmp_path / "address.db")
    run_migrations(engine, database_path=tmp_path / "address.db")

    endpoints = rows(
        engine,
        ADOPTED_ENDPOINT_SQL,
    )
    assert len(endpoints) == 1
    url, auth_kind, envelope = endpoints[0]
    assert ENV_URL in url
    assert auth_kind == "NONE"
    assert envelope is None


def test_thanos_gains_a_connection_instead_of_a_name_shell(tmp_path: Path) -> None:
    """The core F21 gap: HistoricalDataSource used to store no address at all."""
    engine = build_reporter_shape(tmp_path / "thanos.db")
    run_migrations(engine, database_path=tmp_path / "thanos.db")

    sources = rows(engine, "SELECT id, type, lifecycle_state FROM historicaldatasource")
    assert len(sources) == 1
    assert sources[0][1] == "THANOS"
    assert sources[0][2] == "DISABLED"

    revisions = rows(
        engine,
        "SELECT canonical_url, internal_state, timeout_seconds "
        "FROM historicaldatasourcerevision",
    )
    assert revisions == [(ENV_THANOS, "PENDING", 15)]


def test_repointing_is_audited_without_leaking_addresses(tmp_path: Path) -> None:
    engine = build_reporter_shape(tmp_path / "audit.db")
    run_migrations(engine, database_path=tmp_path / "audit.db")

    audits = rows(
        engine,
        "SELECT action, actor, redacted_diff_json, result FROM configaudit "
        "WHERE action = 'ADOPT_LEGACY_SOURCE'",
    )
    assert len(audits) == 4
    seen = set()
    for action, actor, diff_json, result in audits:
        assert (action, actor, result) == ("ADOPT_LEGACY_SOURCE", "migration", "SUCCESS")
        diff = json.loads(diff_json)
        assert diff["rows"] >= 1
        assert "http" not in diff_json
        seen.add((diff["table"], diff["from_source_id"]))
    assert seen == {
        ("alert", LEGACY_A),
        ("alert", LEGACY_B),
        ("incident", LEGACY_A),
        ("incident", LEGACY_B),
    }


def test_adoption_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    engine = build_reporter_shape(path)
    run_migrations(engine, database_path=path)

    before_sources = rows(engine, "SELECT id FROM eventsource ORDER BY id")
    before_audits = rows(engine, "SELECT count(*) FROM configaudit")

    # Re-running the ledger must not adopt a second time.
    second = run_migrations(engine, database_path=path)
    assert second.applied_versions == []

    # Nor may a forced re-execution of the migration body duplicate anything.
    from app.migrations import _migration_6_f21_registry_only_configuration

    with engine.begin() as connection:
        again = _migration_6_f21_registry_only_configuration(connection)

    assert again["adopted_alertmanager"] == 0
    assert again["repointed_rows"] == 0
    assert rows(engine, "SELECT id FROM eventsource ORDER BY id") == before_sources
    assert rows(engine, "SELECT count(*) FROM configaudit") == before_audits


def test_no_configuration_anywhere_adopts_nothing(tmp_path: Path, monkeypatch) -> None:
    """Without an address there is nothing to adopt, and nothing is invented."""
    from app.config import settings

    monkeypatch.setattr(settings, "alertmanager_url", "")
    monkeypatch.setattr(settings, "thanos_url", "")

    path = tmp_path / "empty.db"
    engine = build_reporter_shape(path)
    report = run_migrations(engine, database_path=path)

    details = report.details[6]
    assert details == {
        "adopted_alertmanager": 0,
        "adopted_thanos": 0,
        "repointed_rows": 0,
        "repointed_source_ids": 0,
    }
    # Legacy rows stay where they are rather than pointing at a fabricated source.
    assert sorted(
        source_id for (source_id,) in rows(engine, "SELECT DISTINCT source_id FROM alert")
    ) == sorted([LEGACY_A, LEGACY_B])


def test_active_profile_wins_over_env(tmp_path: Path) -> None:
    """An activated Web connection is a stronger signal than a stale `.env`."""
    path = tmp_path / "profile.db"
    _create_v4_schema(path)
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        # ACTIVE profile: migration 5 already promotes this one to ENABLED,
        # so v6 must leave the registry alone entirely.
        _insert_profile(connection, profile_id=1, source_id=LEGACY_A, state="ACTIVE")

    report = run_migrations(engine, database_path=path)

    assert report.details[6]["adopted_alertmanager"] == 0
    enabled = rows(
        engine, "SELECT count(*) FROM eventsource WHERE lifecycle_state = 'ENABLED'"
    )
    assert enabled == [(1,)]


def test_env_token_is_encrypted_not_stored_in_clear(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "alertmanager_token", "super-secret-token")
    monkeypatch.setattr(settings, "master_key", "unit-test-master-key")

    path = tmp_path / "secret.db"
    engine = build_reporter_shape(path)
    run_migrations(engine, database_path=path)

    endpoints = rows(
        engine,
        ADOPTED_ENDPOINT_SQL,
    )
    _url, auth_kind, envelope_json = endpoints[0]
    assert auth_kind == "BEARER"
    assert envelope_json is not None
    assert "super-secret-token" not in envelope_json
    envelope = json.loads(envelope_json)
    assert envelope["algorithm"] == "fernet"


def test_missing_master_key_drops_the_token_instead_of_blocking_startup(
    tmp_path: Path, monkeypatch
) -> None:
    """Failing the upgrade over a missing key would make the app unstartable."""
    from app.config import settings

    monkeypatch.setattr(settings, "alertmanager_token", "super-secret-token")
    monkeypatch.setattr(settings, "master_key", "")

    path = tmp_path / "nokey.db"
    engine = build_reporter_shape(path)
    report = run_migrations(engine, database_path=path)

    assert report.details[6]["adopted_alertmanager"] == 1
    endpoints = rows(
        engine,
        ADOPTED_ENDPOINT_SQL,
    )
    assert [(a, e) for _u, a, e in endpoints] == [("NONE", None)]
    # The address survived; only the credential needs re-entry before enabling.
    urls = rows(engine, ADOPTED_ENDPOINT_SQL)
    assert ENV_URL in urls[0][0]


def test_v1_to_v5_checksums_stay_frozen() -> None:
    from app.migrations import MIGRATIONS
    from test_f20_migrations import FROZEN_MIGRATION_CHECKSUMS

    assert {
        migration.version: migration.checksum for migration in MIGRATIONS[:5]
    } == FROZEN_MIGRATION_CHECKSUMS
