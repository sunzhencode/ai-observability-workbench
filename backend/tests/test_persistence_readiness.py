"""Persistence readiness probes expose only stable safe codes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.bootstrap import create_platform_app, default_platform_wiring
from app.platform.persistence.database import SqliteDatabaseConfig, create_sqlite_engine
from app.platform.persistence.migrations import upgrade_database
from app.platform.persistence.probes import create_persistence_readiness_probes


def test_persistence_readiness_checks_database_migration_disk_and_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / "incident-operations.db"
    key = tmp_path / "master.key"
    key.write_text("readable-existing-key\n", encoding="utf-8")
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=database))
    upgrade_database(engine)
    wiring = default_platform_wiring(
        readiness_probes=create_persistence_readiness_probes(
            engine=engine,
            database_path=database,
            master_key_path=key,
        )
    )

    with TestClient(create_platform_app(wiring=wiring)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == [
        {"name": "database", "status": "ready", "code": "OK"},
        {"name": "migration", "status": "ready", "code": "OK"},
        {"name": "disk", "status": "ready", "code": "OK"},
        {"name": "master_key", "status": "ready", "code": "OK"},
    ]


def test_missing_key_and_stale_migration_are_typed_without_raw_paths(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private-name.db"
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=database))
    probes = create_persistence_readiness_probes(
        engine=engine,
        database_path=database,
        master_key_path=tmp_path / "missing-secret-name.key",
    )
    wiring = default_platform_wiring(readiness_probes=probes)

    with TestClient(create_platform_app(wiring=wiring)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    by_name = {item["name"]: item for item in body["checks"]}
    assert by_name["migration"]["code"] == "MIGRATION_NOT_CURRENT"
    assert by_name["master_key"]["code"] == "MASTER_KEY_UNREADABLE"
    assert "private-name" not in response.text
    assert "missing-secret-name" not in response.text
