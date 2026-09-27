"""The destructive persistence CLI is dry-run first."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.platform.persistence.cli import main
from app.platform.persistence.inventory import build_configuration_inventory


def _state(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "workbench.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE current_product (id INTEGER PRIMARY KEY)")
    key = tmp_path / "master.key"
    key.write_text("existing-key\n", encoding="utf-8")
    key.chmod(0o600)
    return database, key


def test_reset_command_defaults_to_a_non_mutating_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, key = _state(tmp_path)

    exit_code = main(
        [
            "reset",
            "--database",
            str(database),
            "--master-key",
            str(key),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["executed"] is False
    assert database.exists()
    assert key.read_text(encoding="utf-8") == "existing-key\n"


def test_reset_execute_requires_stopped_process_confirmation(
    tmp_path: Path,
) -> None:
    database, key = _state(tmp_path)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "reset",
                "--database",
                str(database),
                "--master-key",
                str(key),
                "--execute",
            ]
        )

    assert raised.value.code == 2
    assert database.exists()


def test_inventory_lists_recreation_objects_without_secret_bearing_fields(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-workbench.db"
    secret = "secret-value-that-must-not-escape"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE eventsource (
                id TEXT PRIMARY KEY, name TEXT, lifecycle_state TEXT,
                active_revision_id INTEGER, version INTEGER
            );
            CREATE TABLE eventsourcerevision (
                id INTEGER PRIMARY KEY, source_id TEXT
            );
            CREATE TABLE alertmanagerendpointrevision (
                id INTEGER PRIMARY KEY, source_revision_id INTEGER,
                canonical_url TEXT, username TEXT, secret_envelope_json TEXT
            );
            CREATE TABLE sourcethanosconfig (
                id INTEGER PRIMARY KEY, source_id TEXT, canonical_url TEXT,
                username TEXT, secret_envelope_json TEXT
            );
            CREATE TABLE sourcegrafanaconfig (
                id INTEGER PRIMARY KEY, source_id TEXT, base_url TEXT,
                secret_envelope_json TEXT
            );
            CREATE TABLE aggregationrule (
                id INTEGER PRIMARY KEY, name TEXT, priority INTEGER,
                scope_mode TEXT, enabled INTEGER, version INTEGER,
                matchers TEXT
            );
            CREATE TABLE aggregationrulesource (rule_id INTEGER, source_id TEXT);
            CREATE TABLE metricquerytemplate (
                id INTEGER PRIMARY KEY, name TEXT, promql TEXT,
                builtin_key TEXT, user_modified INTEGER, enabled INTEGER
            );
            CREATE TABLE metrictemplateorigin (template_id INTEGER);
            CREATE TABLE metrictemplatesourcescope (
                template_id INTEGER PRIMARY KEY, scope_mode TEXT,
                source_ids_json TEXT
            );
            CREATE TABLE modelchannel (
                id INTEGER PRIMARY KEY, name TEXT, kind TEXT, enabled INTEGER
            );
            CREATE TABLE modelchannelrevision (
                id INTEGER PRIMARY KEY, channel_id INTEGER, state TEXT,
                config_envelope_json TEXT, secret_envelope_json TEXT
            );
            CREATE TABLE notificationchannel (
                id INTEGER PRIMARY KEY, name TEXT, state TEXT,
                active_revision_id INTEGER
            );
            CREATE TABLE notificationchannelrevision (
                id INTEGER PRIMARY KEY, channel_id INTEGER, provider TEXT,
                webhook_envelope TEXT, signing_secret_envelope TEXT
            );
            CREATE TABLE notificationpolicyrevision (
                id INTEGER PRIMARY KEY, logical_id INTEGER, version INTEGER,
                name TEXT, state TEXT, priority INTEGER, scope_mode TEXT,
                matchers TEXT
            );
            CREATE TABLE notificationpolicysource (
                policy_revision_id INTEGER, source_id TEXT
            );
            CREATE TABLE notificationpolicychannel (
                policy_revision_id INTEGER, channel_id INTEGER
            );
            CREATE TABLE runtimesetting (key TEXT PRIMARY KEY, value_json TEXT);
            """
        )
        connection.execute(
            "INSERT INTO eventsource VALUES ('source-a', 'Primary', 'ENABLED', 1, 3)"
        )
        connection.execute("INSERT INTO eventsourcerevision VALUES (1, 'source-a')")
        connection.execute(
            "INSERT INTO alertmanagerendpointrevision VALUES (1, 1, ?, ?, ?)",
            ("https://private.invalid", secret, secret),
        )
        connection.execute(
            "INSERT INTO sourcethanosconfig VALUES (1, 'source-a', ?, ?, ?)",
            ("https://thanos.invalid", secret, secret),
        )
        connection.execute(
            "INSERT INTO sourcegrafanaconfig VALUES (1, 'source-a', ?, ?)",
            ("https://grafana.invalid", secret),
        )
        connection.execute(
            "INSERT INTO runtimesetting VALUES ('workbench_url', ?)",
            (f'"https://{secret}.invalid"',),
        )

    payload = build_configuration_inventory(
        database,
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    rendered = json.dumps(payload)

    assert payload["secrets_included"] is False
    assert payload["objects"]["event_sources"] == [
        {
            "id": "source-a",
            "name": "Primary",
            "lifecycle_state": "ENABLED",
            "version": 3,
            "endpoint_count": 1,
            "thanos_configured": 1,
            "grafana_configured": 1,
        }
    ]
    assert payload["objects"]["workbench_url"] == {"configured": True}
    assert secret not in rendered
    assert "private.invalid" not in rendered


def test_inventory_cli_is_read_only_and_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, _ = _state(tmp_path)
    before = database.read_bytes()

    exit_code = main(["inventory", "--database", str(database)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["secrets_included"] is False
    assert database.read_bytes() == before
