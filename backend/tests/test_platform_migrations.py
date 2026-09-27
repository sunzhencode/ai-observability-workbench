"""Alembic baseline and immutable revision manifest contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.platform.persistence.database import SqliteDatabaseConfig, create_sqlite_engine
from app.platform.persistence.migrations import (
    MigrationManifestError,
    current_revision,
    downgrade_database,
    head_revision,
    migration_is_current,
    upgrade_database,
    verify_revision_manifest,
)


def test_baseline_upgrades_and_downgrades_an_empty_temp_database(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))

    assert current_revision(engine) is None
    upgrade_database(engine)

    assert migration_is_current(engine) is True
    assert current_revision(engine) == head_revision()
    tables = set(inspect(engine).get_table_names())
    assert {
        "alert",
        "event_source",
        "operational_occurrence",
        "incident_timeline_entry",
        "incident_task",
        "incident_note_content",
        "investigation",
        "investigation_retention_gate",
        "initial_investigation_snapshot",
        "investigation_trajectory_head",
        "evidence_brief_v1",
        "metric_observation_v1",
        "metric_empty_observation_v1",
        "investigation_degradation_v1",
        "investigation_daily_usage_v1",
        "provider_profile",
        "investigation_run_v2",
        "evidence_snapshot_v2",
        "investigation_activity_v2",
        "investigation_report_v2",
        "investigation_tool_scope_v2",
        "investigation_metric_observation_v2",
        "operations_retention_gate",
        "platform_job",
        "platform_setting",
    } <= tables
    assert "notificationchannel" not in tables
    assert "schemamigration" not in tables
    assert "current_response_state" in {
        column["name"] for column in inspect(engine).get_columns("notification_route")
    }
    execution_mode = next(
        column
        for column in inspect(engine).get_columns("investigation")
        if column["name"] == "model_execution_mode"
    )
    assert execution_mode["nullable"] is False
    assert execution_mode["default"] == "'UNKNOWN_LEGACY'"
    assert "provider_profile_id" in {
        column["name"]
        for column in inspect(engine).get_columns("model_channel_revision")
    }
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='incident_timeline_entry'"
                )
            )
        }
        timeline_delete_trigger = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name='incident_timeline_no_delete'"
            )
        )
    assert triggers == {"incident_timeline_no_update", "incident_timeline_no_delete"}
    assert timeline_delete_trigger is not None
    assert "operations_retention_gate" in timeline_delete_trigger

    downgrade_database(engine, revision="base")
    assert current_revision(engine) is None


def test_migration_refuses_a_legacy_v1_to_v15_database(tmp_path: Path) -> None:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "legacy.db"))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE schemamigration (version INTEGER)"))

    with pytest.raises(RuntimeError, match="legacy v1-v15"):
        upgrade_database(engine)


def test_unified_investigator_migration_adopts_existing_model_channels(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "legacy-model-channel.db")
    )
    upgrade_database(engine, revision="platform_0022")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO model_channel (
                  id,name,kind,enabled,active_revision_id,created_at,updated_at
                ) VALUES (
                  'model-kimi','kimi','OPENAI_COMPATIBLE',1,NULL,
                  '2026-09-01 00:00:00','2026-09-01 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO model_channel_revision (
                  id,channel_id,revision_no,state,base_url,model,api_key_envelope,
                  tested_at,last_test_code,created_at
                ) VALUES (
                  41,'model-kimi',2,'ACTIVE','https://api.moonshot.cn/v1',
                  'kimi-k2.6',NULL,NULL,'MODEL_SERVICE_UNAVAILABLE',
                  '2026-09-01 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                "UPDATE model_channel SET active_revision_id=41 WHERE id='model-kimi'"
            )
        )

    upgrade_database(engine)

    with engine.connect() as connection:
        adopted = connection.execute(
            text(
                """
                SELECT revision.provider_profile_id, profile.provider_id,
                       profile.protocol_profile, profile.base_url,
                       profile.settings_json
                FROM model_channel_revision AS revision
                LEFT JOIN provider_profile AS profile
                  ON profile.id = revision.provider_profile_id
                WHERE revision.id = 41
                """
            )
        ).one()
    assert adopted.provider_profile_id is not None
    assert adopted.provider_id == "MOONSHOT"
    assert adopted.protocol_profile == "CHAT_COMPLETIONS"
    assert adopted.base_url == "https://api.moonshot.cn/v1"
    assert adopted.settings_json == (
        '{"extra_body":{"thinking":{"type":"disabled"}},'
        '"parallel_tool_calls":false}'
    )


def test_response_lifecycle_migration_maps_legacy_state_and_tightens_constraint(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "response-lifecycle.db")
    )
    upgrade_database(engine, revision="platform_0012")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO event_source (
                  id,name,state,version,poll_interval_seconds,resolution_grace_seconds,
                  max_parallel_endpoints,watchdog_enabled,watchdog_alertname,
                  watchdog_identity_label,watchdog_missing_after_seconds,created_at,updated_at
                ) VALUES (
                  'src-a','Primary','ENABLED',1,30,0,2,0,'Watchdog','cluster',60,
                  '2026-08-24 00:00:00','2026-08-24 00:00:00'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO incident (
                  id,source_id,group_key,title,severity,source_state,freshness_state,
                  group_labels_json,missing_labels_json,occurrence_no,
                  occurrence_started_at,updated_at,handling_state,handling_version,
                  change_version,change_origin
                ) VALUES (
                  1,'src-a','source=src-a|alert=test','Test','warning','FIRING','FRESH',
                  '{}','[]',1,'2026-08-24 00:00:00','2026-08-24 00:00:00',
                  'IN_PROGRESS',1,1,'LIVE_POLL'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO operational_occurrence (
                  id,incident_id,occurrence_no,source_id,title,signal_state,
                  signal_severity,response_state,response_priority,assignment_origin,
                  ack_sla_seconds,latest_activity_at,version,group_key,member_count,
                  evidence_completeness
                ) VALUES (
                  1,1,1,'src-a','Test','FIRING','warning','MITIGATING','P2',
                  'UNMAPPED',900,'2026-08-24 00:00:00',4,
                  'source=src-a|alert=test',1,'COMPLETE'
                )
                """
            )
        )

    upgrade_database(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT response_state FROM operational_occurrence WHERE id=1")
        ).scalar_one() == "IN_PROGRESS"
        table_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='operational_occurrence'"
            )
        ).scalar_one()
    assert "'IN_PROGRESS'" in table_sql
    assert "'MITIGATING'" not in table_sql

    downgrade_database(engine, revision="platform_0012")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT response_state FROM operational_occurrence WHERE id=1")
        ).scalar_one() == "ACKNOWLEDGED"
    engine.dispose()


def test_committed_revision_manifest_detects_drift(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "platform"
        / "persistence"
        / "alembic"
    )
    copied = tmp_path / "alembic_platform"
    shutil.copytree(source, copied)

    verify_revision_manifest(script_location=copied)
    revision = copied / "versions" / "0001_platform_baseline.py"
    revision.write_text(
        revision.read_text(encoding="utf-8") + "\n# rewritten history\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationManifestError, match="checksum"):
        verify_revision_manifest(script_location=copied)
