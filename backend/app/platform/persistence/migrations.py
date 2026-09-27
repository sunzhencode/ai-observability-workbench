"""Alembic runner and immutable revision checks for the new Incident Operations database."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

DEFAULT_SCRIPT_LOCATION = Path(__file__).resolve().parent / "alembic"
MANIFEST_FILENAME = "revision-manifest.json"


class MigrationManifestError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_revision_manifest(
    *, script_location: Path = DEFAULT_SCRIPT_LOCATION
) -> None:
    manifest_path = script_location / MANIFEST_FILENAME
    try:
        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        revisions = raw["revisions"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise MigrationManifestError("Incident Operations revision manifest is invalid") from exc
    if not isinstance(revisions, dict) or not all(
        isinstance(name, str) and isinstance(checksum, str)
        for name, checksum in revisions.items()
    ):
        raise MigrationManifestError("Incident Operations revision manifest is invalid")

    actual_files = {
        path.name: path
        for path in (script_location / "versions").glob("*.py")
        if path.name != "__init__.py"
    }
    if set(actual_files) != set(revisions):
        raise MigrationManifestError("Incident Operations revision manifest file set changed")
    for name, path in actual_files.items():
        if _sha256(path) != revisions[name]:
            raise MigrationManifestError(f"Incident Operations revision checksum mismatch: {name}")


def _config(script_location: Path = DEFAULT_SCRIPT_LOCATION) -> Config:
    config = Config()
    config.set_main_option("script_location", str(script_location))
    return config


def head_revision(*, script_location: Path = DEFAULT_SCRIPT_LOCATION) -> str:
    verify_revision_manifest(script_location=script_location)
    head = ScriptDirectory.from_config(_config(script_location)).get_current_head()
    if head is None:
        raise MigrationManifestError("Incident Operations migration head is missing")
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def migration_is_current(
    engine: Engine,
    *, script_location: Path = DEFAULT_SCRIPT_LOCATION,
) -> bool:
    return current_revision(engine) == head_revision(script_location=script_location)


def _assert_not_legacy_database(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    if "schemamigration" in tables and "alembic_version" not in tables:
        raise RuntimeError(
            "refusing to run Incident Operations Alembic against a legacy v1-v15 database"
        )


def upgrade_database(
    engine: Engine,
    *,
    revision: str = "head",
    script_location: Path = DEFAULT_SCRIPT_LOCATION,
) -> None:
    verify_revision_manifest(script_location=script_location)
    _assert_not_legacy_database(engine)
    config = _config(script_location)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade_database(
    engine: Engine,
    *,
    revision: str,
    script_location: Path = DEFAULT_SCRIPT_LOCATION,
) -> None:
    verify_revision_manifest(script_location=script_location)
    config = _config(script_location)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)
