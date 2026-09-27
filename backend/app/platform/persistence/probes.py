"""Readiness probes for the Incident Operations SQLite persistence foundation."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import Engine

from app.platform.health import ReadinessProbe, ReadinessProbeResult
from app.platform.persistence.migrations import migration_is_current

DEFAULT_MINIMUM_FREE_BYTES = 10 * 1024 * 1024


def create_persistence_readiness_probes(
    *,
    engine: Engine,
    database_path: Path,
    master_key_path: Path,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
) -> tuple[ReadinessProbe, ...]:
    database = Path(database_path).expanduser().resolve()
    key = Path(master_key_path).expanduser().resolve()

    async def database_check() -> ReadinessProbeResult:
        raw_connection = engine.raw_connection()
        try:
            cursor = raw_connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("ROLLBACK")
            finally:
                cursor.close()
        finally:
            raw_connection.close()
        return ReadinessProbeResult()

    async def migration_check() -> ReadinessProbeResult:
        if not migration_is_current(engine):
            return ReadinessProbeResult(
                status="not_ready",
                code="MIGRATION_NOT_CURRENT",
            )
        return ReadinessProbeResult()

    async def disk_check() -> ReadinessProbeResult:
        if shutil.disk_usage(database.parent).free < minimum_free_bytes:
            return ReadinessProbeResult(status="not_ready", code="DISK_SPACE_LOW")
        return ReadinessProbeResult()

    async def master_key_check() -> ReadinessProbeResult:
        try:
            with key.open("rb") as handle:
                readable = bool(handle.read(1))
        except OSError:
            readable = False
        if not readable:
            return ReadinessProbeResult(
                status="not_ready",
                code="MASTER_KEY_UNREADABLE",
            )
        return ReadinessProbeResult()

    return (
        ReadinessProbe(
            name="database",
            check=database_check,
            failure_code="DATABASE_READINESS_FAILED",
        ),
        ReadinessProbe(
            name="migration",
            check=migration_check,
            failure_code="MIGRATION_READINESS_FAILED",
        ),
        ReadinessProbe(
            name="disk",
            check=disk_check,
            failure_code="DISK_READINESS_FAILED",
        ),
        ReadinessProbe(
            name="master_key",
            check=master_key_check,
            failure_code="MASTER_KEY_READINESS_FAILED",
        ),
    )
