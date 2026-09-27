"""F22 migration v8: one configuration per source, Thanos absorbed onto it.

The upgrade has to survive the two shapes that actually exist in the wild: a
source carrying both an applied and an unapplied configuration, and a source
whose Thanos address lives behind an evidence binding.  It also has to survive
a database whose master key is gone, because it never decrypts anything.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app import migrations as migration_module
from app.db import create_sqlite_engine
from app.migrations import run_migrations

NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


def rows(engine, sql: str, params: dict | None = None) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql), params or {}).all()]


@pytest.fixture
def pre_f22(tmp_path: Path, monkeypatch):
    """A database migrated to v7 only, i.e. the state F22 has to upgrade."""
    monkeypatch.setattr(
        migration_module,
        "MIGRATIONS",
        tuple(item for item in migration_module.MIGRATIONS if item.version < 8),
    )
    path = tmp_path / "pre-f22.db"
    engine = create_sqlite_engine(path)
    run_migrations(engine, database_path=path)
    monkeypatch.undo()
    return engine, path


def add_source(engine, source_id: str, name: str, state: str = "ENABLED") -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO eventsource (id, type, name, lifecycle_state, "
                "active_revision_id, created_at, updated_at, version) VALUES "
                "(:id, 'ALERTMANAGER', :name, :state, NULL, :now, :now, 1)"
            ),
            {"id": source_id, "name": name, "state": state, "now": NOW},
        )


def add_revision(
    engine, source_id: str, revision_no: int, internal_state: str
) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            text(
                "INSERT INTO eventsourcerevision (source_id, revision_no, "
                "internal_state, poll_interval_seconds, resolution_grace_seconds, "
                "max_parallel_endpoints, watchdog_enabled, watchdog_alertname, "
                "watchdog_identity_label, watchdog_missing_after_seconds, "
                "created_at) VALUES (:source, :no, :state, 30, 60, 4, 0, "
                "'Watchdog', 'cluster', 90, :now)"
            ),
            {
                "source": source_id,
                "no": revision_no,
                "state": internal_state,
                "now": NOW,
            },
        )
        revision_id = int(result.lastrowid)
        connection.execute(
            text(
                "INSERT INTO alertmanagerendpointrevision (source_revision_id, "
                "position, canonical_url, enabled, auth_kind, username) VALUES "
                "(:revision, 0, :url, 1, 'NONE', '')"
            ),
            {"revision": revision_id, "url": f"http://am-{revision_no}.test"},
        )
    return revision_id


def add_thanos_binding(
    engine,
    source_id: str,
    historical_id: str,
    url: str,
    *,
    enabled: bool = True,
    binding_state: str = "ACTIVE",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO historicaldatasource (id, type, name, "
                "lifecycle_state, active_revision_id, created_at, updated_at, "
                "version) VALUES (:id, 'THANOS', :id, 'ENABLED', NULL, :now, "
                ":now, 1)"
            ),
            {"id": historical_id, "now": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO historicaldatasourcerevision (historical_source_id, "
                "revision_no, internal_state, canonical_url, auth_kind, username, "
                "secret_envelope_json, timeout_seconds, created_at) VALUES "
                "(:id, 1, :state, :url, 'BEARER', '', :secret, 20, :now)"
            ),
            {
                "id": historical_id,
                "state": binding_state,
                "url": url,
                "secret": '{"v": 1, "ciphertext": "opaque"}',
                "now": NOW,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sourceevidencebinding (source_id, "
                "historical_source_id, scope_mode, matchers_json, enabled, "
                "version, last_preview_summary) VALUES "
                "(:source, :historical, 'UNSCOPED', '[]', :enabled, 1, '{}')"
            ),
            {
                "source": source_id,
                "historical": historical_id,
                "enabled": 1 if enabled else 0,
            },
        )


def test_active_wins_and_the_unapplied_change_is_discarded_with_a_trail(
    pre_f22,
) -> None:
    engine, path = pre_f22
    add_source(engine, "src_both", "both")
    active = add_revision(engine, "src_both", 1, "ACTIVE")
    add_revision(engine, "src_both", 2, "PENDING")

    run_migrations(engine, database_path=path)

    assert rows(
        engine,
        "SELECT id FROM eventsourcerevision WHERE source_id = 'src_both' "
        "AND internal_state = 'ACTIVE'",
    ) == [(active,)]
    assert rows(
        engine, "SELECT active_revision_id FROM eventsource WHERE id = 'src_both'"
    ) == [(active,)]
    assert rows(
        engine,
        "SELECT action FROM configaudit WHERE resource_id = 'src_both' "
        "AND action = 'F22_PENDING_DISCARDED'",
    ) == [("F22_PENDING_DISCARDED",)]


def test_a_source_that_was_never_applied_keeps_its_only_configuration(
    pre_f22,
) -> None:
    engine, path = pre_f22
    add_source(engine, "src_pending", "pending only", state="DISABLED")
    pending = add_revision(engine, "src_pending", 1, "PENDING")

    run_migrations(engine, database_path=path)

    assert rows(
        engine,
        "SELECT internal_state, id FROM eventsourcerevision "
        "WHERE source_id = 'src_pending'",
    ) == [("ACTIVE", pending)]
    # Promoting the configuration must not promote the source itself.
    assert rows(
        engine, "SELECT lifecycle_state FROM eventsource WHERE id = 'src_pending'"
    ) == [("DISABLED",)]


def test_superseded_rows_are_retired_not_deleted(pre_f22) -> None:
    """A referenced configuration row must survive; deleting it breaks startup."""
    engine, path = pre_f22
    add_source(engine, "src_hist", "history")
    old = add_revision(engine, "src_hist", 1, "RETIRED")
    current = add_revision(engine, "src_hist", 2, "ACTIVE")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sourcepollrun (source_id, source_revision_id, "
                "started_at, completeness, endpoint_total, endpoint_succeeded, "
                "endpoint_failed, normalized_alert_count, safe_error_codes) "
                "VALUES ('src_hist', :revision, :now, 'COMPLETE', 1, 1, 0, 3, '[]')"
            ),
            {"revision": old, "now": NOW},
        )

    run_migrations(engine, database_path=path)

    states = dict(
        rows(
            engine,
            "SELECT id, internal_state FROM eventsourcerevision "
            "WHERE source_id = 'src_hist'",
        )
    )
    assert states == {old: "RETIRED", current: "ACTIVE"}
    assert rows(
        engine, "SELECT source_revision_id FROM sourcepollrun"
    ) == [(old,)]


def test_thanos_binding_lands_on_the_source_with_the_ciphertext_intact(
    pre_f22,
) -> None:
    engine, path = pre_f22
    add_source(engine, "src_thanos", "with history")
    add_revision(engine, "src_thanos", 1, "ACTIVE")
    add_thanos_binding(engine, "src_thanos", "hist_a", "http://thanos.test")

    run_migrations(engine, database_path=path)

    assert rows(
        engine,
        "SELECT source_id, canonical_url, auth_kind, secret_envelope_json, "
        "timeout_seconds FROM sourcethanosconfig",
    ) == [
        (
            "src_thanos",
            "http://thanos.test",
            "BEARER",
            '{"v": 1, "ciphertext": "opaque"}',
            20,
        )
    ]


def test_only_the_first_binding_survives_and_the_rest_are_audited(
    pre_f22,
) -> None:
    engine, path = pre_f22
    add_source(engine, "src_multi", "two bindings")
    add_revision(engine, "src_multi", 1, "ACTIVE")
    add_thanos_binding(engine, "src_multi", "hist_first", "http://first.test")
    add_thanos_binding(engine, "src_multi", "hist_second", "http://second.test")

    run_migrations(engine, database_path=path)

    assert rows(
        engine, "SELECT canonical_url FROM sourcethanosconfig"
    ) == [("http://first.test",)]
    assert rows(
        engine,
        "SELECT action FROM configaudit WHERE action = 'F22_BINDING_DROPPED'",
    ) == [("F22_BINDING_DROPPED",)]


def test_a_disabled_binding_does_not_become_a_history_address(pre_f22) -> None:
    engine, path = pre_f22
    add_source(engine, "src_off", "binding off")
    add_revision(engine, "src_off", 1, "ACTIVE")
    add_thanos_binding(
        engine, "src_off", "hist_off", "http://off.test", enabled=False
    )

    run_migrations(engine, database_path=path)

    assert rows(engine, "SELECT source_id FROM sourcethanosconfig") == []


def test_upgrade_needs_no_master_key(pre_f22, monkeypatch) -> None:
    """Ciphertext is carried verbatim, so a lost key cannot block startup."""
    from app.config import settings

    monkeypatch.setattr(settings, "master_key", "")
    engine, path = pre_f22
    add_source(engine, "src_nokey", "no key")
    add_revision(engine, "src_nokey", 1, "ACTIVE")
    add_thanos_binding(engine, "src_nokey", "hist_nokey", "http://thanos.test")

    report = run_migrations(engine, database_path=path)

    assert 8 in report.applied_versions
    assert rows(engine, "SELECT canonical_url FROM sourcethanosconfig") == [
        ("http://thanos.test",)
    ]


def test_repeated_startups_are_stable(pre_f22) -> None:
    engine, path = pre_f22
    add_source(engine, "src_stable", "stable")
    add_revision(engine, "src_stable", 1, "ACTIVE")
    add_revision(engine, "src_stable", 2, "PENDING")
    add_thanos_binding(engine, "src_stable", "hist_stable", "http://thanos.test")

    first = run_migrations(engine, database_path=path)
    from app.migrations import MIGRATIONS

    assert first.applied_versions[-1] == MIGRATIONS[-1].version
    for _ in range(2):
        assert run_migrations(engine, database_path=path).applied_versions == []

    assert len(rows(engine, "SELECT id FROM sourcethanosconfig")) == 1
    assert len(
        rows(
            engine,
            "SELECT id FROM eventsourcerevision WHERE source_id = 'src_stable' "
            "AND internal_state = 'ACTIVE'",
        )
    ) == 1
