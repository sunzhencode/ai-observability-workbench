"""Explicit database archive, reset and recovery safety contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.platform.persistence.archive import (
    ArchiveSafetyError,
    archive_database,
    plan_database_reset,
    reset_database,
    restore_database,
)
from app.platform.persistence.database import SqliteDatabaseConfig, create_sqlite_engine
from app.platform.persistence.migrations import head_revision, migration_is_current


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "workbench.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE preserved (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved (value) VALUES ('kept')")
    Path(f"{database}-wal").write_bytes(b"wal-bytes")
    Path(f"{database}-shm").write_bytes(b"shm-bytes")
    key = tmp_path / "master.key"
    key.write_text("existing-key-must-not-change\n", encoding="utf-8")
    key.chmod(0o600)
    return database, key


def test_reset_plan_is_read_only_and_lists_only_the_sqlite_file_set(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)

    plan = plan_database_reset(
        database_path=database,
        archive_root=tmp_path / "archive",
        master_key_path=key,
        now=datetime(2026, 8, 11, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert plan.archive_directory.name == "pre-incident-operations-20260811T010203Z"
    assert [item.source.name for item in plan.artifacts] == [
        "workbench.db",
        "workbench.db-wal",
        "workbench.db-shm",
    ]
    assert database.exists()
    assert key.read_text(encoding="utf-8") == "existing-key-must-not-change\n"
    assert not plan.archive_directory.exists()
    assert "existing-key" not in json.dumps(plan.as_dict())


def test_archive_requires_explicit_stopped_process_confirmation(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)
    plan = plan_database_reset(database_path=database, master_key_path=key)

    with pytest.raises(ArchiveSafetyError, match="process is stopped"):
        archive_database(plan, confirm_process_stopped=False)

    assert database.exists()
    assert key.exists()


def test_archive_moves_db_wal_shm_writes_checksums_and_never_moves_key(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)
    database.chmod(0o777)
    Path(f"{database}-wal").chmod(0o777)
    Path(f"{database}-shm").chmod(0o777)
    original_key = key.read_bytes()
    plan = plan_database_reset(
        database_path=database,
        master_key_path=key,
        now=datetime(2026, 8, 11, 1, 2, 3, tzinfo=timezone.utc),
    )

    manifest = archive_database(plan, confirm_process_stopped=True)

    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
    assert key.read_bytes() == original_key
    assert (plan.archive_directory / "workbench.db").read_bytes().startswith(
        b"SQLite format 3"
    )
    assert (plan.archive_directory / "workbench.db-wal").read_bytes() == b"wal-bytes"
    assert (plan.archive_directory / "workbench.db-shm").read_bytes() == b"shm-bytes"
    assert plan.archive_directory.stat().st_mode & 0o777 == 0o700
    assert all(
        (plan.archive_directory / name).stat().st_mode & 0o777 == 0o600
        for name in (
            "workbench.db",
            "workbench.db-wal",
            "workbench.db-shm",
            "manifest.json",
        )
    )
    persisted = json.loads((plan.archive_directory / "manifest.json").read_text())
    assert persisted == manifest.as_dict()
    assert all(len(item["sha256"]) == 64 for item in persisted["artifacts"])
    assert persisted["master_key"]["moved"] is False
    assert "existing-key" not in json.dumps(persisted)


def test_restore_verifies_manifest_and_refuses_to_overwrite_a_target(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)
    plan = plan_database_reset(database_path=database, master_key_path=key)
    archive_database(plan, confirm_process_stopped=True)

    database.write_bytes(b"new-database-that-must-not-be-overwritten")
    with pytest.raises(ArchiveSafetyError, match="already exists"):
        restore_database(
            archive_directory=plan.archive_directory,
            database_path=database,
            master_key_path=key,
            confirm_process_stopped=True,
        )

    assert database.read_bytes() == b"new-database-that-must-not-be-overwritten"


def test_restore_recreates_the_exact_file_set_without_consuming_archive(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)
    plan = plan_database_reset(database_path=database, master_key_path=key)
    archive_database(plan, confirm_process_stopped=True)
    original_key = key.read_bytes()

    restored = restore_database(
        archive_directory=plan.archive_directory,
        database_path=database,
        master_key_path=key,
        confirm_process_stopped=True,
    )

    assert restored == (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
    assert database.read_bytes().startswith(b"SQLite format 3")
    assert Path(f"{database}-wal").read_bytes() == b"wal-bytes"
    assert Path(f"{database}-shm").read_bytes() == b"shm-bytes"
    assert (plan.archive_directory / "manifest.json").exists()
    assert key.read_bytes() == original_key


def test_preflight_fails_closed_when_master_key_is_missing(tmp_path: Path) -> None:
    database = tmp_path / "workbench.db"
    database.write_bytes(b"database")

    with pytest.raises(ArchiveSafetyError, match="master.key"):
        plan_database_reset(database_path=database)


def test_preflight_refuses_to_treat_the_database_as_master_key(tmp_path: Path) -> None:
    database = tmp_path / "workbench.db"
    database.write_bytes(b"not-a-key")

    with pytest.raises(ArchiveSafetyError, match="separate"):
        plan_database_reset(database_path=database, master_key_path=database)


def test_archive_refuses_a_wal_that_appears_after_preflight(tmp_path: Path) -> None:
    database, key = _fixture(tmp_path)
    Path(f"{database}-wal").unlink()
    Path(f"{database}-shm").unlink()
    plan = plan_database_reset(database_path=database, master_key_path=key)
    Path(f"{database}-wal").write_bytes(b"late-wal")

    with pytest.raises(ArchiveSafetyError, match="changed after preflight"):
        archive_database(plan, confirm_process_stopped=True)

    assert database.exists()


def test_reset_archives_old_set_then_initializes_the_current_platform_head(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)
    original_key = key.read_bytes()
    plan = plan_database_reset(database_path=database, master_key_path=key)

    result = reset_database(plan, confirm_process_stopped=True)

    engine = create_sqlite_engine(SqliteDatabaseConfig(path=database))
    try:
        assert migration_is_current(engine) is True
    finally:
        engine.dispose()
    assert result.migration_revision == head_revision()
    assert (plan.archive_directory / "workbench.db").exists()
    assert key.read_bytes() == original_key


def test_restore_rejects_tampered_archived_bytes(tmp_path: Path) -> None:
    database, key = _fixture(tmp_path)
    plan = plan_database_reset(database_path=database, master_key_path=key)
    archive_database(plan, confirm_process_stopped=True)
    (plan.archive_directory / "workbench.db").write_bytes(b"tampered")

    with pytest.raises(ArchiveSafetyError, match="checksum"):
        restore_database(
            archive_directory=plan.archive_directory,
            database_path=database,
            master_key_path=key,
            confirm_process_stopped=True,
        )


def test_full_reset_then_manual_rollback_restores_the_legacy_database(
    tmp_path: Path,
) -> None:
    database, key = _fixture(tmp_path)
    original_key = key.read_bytes()
    plan = plan_database_reset(database_path=database, master_key_path=key)

    result = reset_database(plan, confirm_process_stopped=True)
    candidate = tmp_path / "candidate-after-reset.db"
    database.replace(candidate)
    restored = restore_database(
        archive_directory=plan.archive_directory,
        database_path=database,
        master_key_path=key,
        confirm_process_stopped=True,
    )

    assert result.migration_revision == head_revision()
    assert candidate.exists()
    assert restored[0] == database
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM preserved").fetchone() == (
            "kept",
        )
    assert key.read_bytes() == original_key
