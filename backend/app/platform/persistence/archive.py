"""Explicit, recoverable archive/reset operations for the Incident Operations cutover."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.platform.persistence.database import SqliteDatabaseConfig, create_sqlite_engine
from app.platform.persistence.migrations import current_revision, upgrade_database

ArtifactRole = Literal["database", "wal", "shm"]
MINIMUM_FREE_BYTES = 1024 * 1024
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class ArchiveSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveArtifactPlan:
    role: ArtifactRole
    source: Path
    destination: Path
    size_bytes: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "role": self.role,
            "source": str(self.source),
            "destination": str(self.destination),
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    database_path: Path
    archive_directory: Path
    master_key_path: Path
    created_at: str
    artifacts: tuple[ArchiveArtifactPlan, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": "archive_and_initialize_incident_operations",
            "database_path": str(self.database_path),
            "archive_directory": str(self.archive_directory),
            "created_at": self.created_at,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "master_key": {"path": str(self.master_key_path), "moved": False},
            "executed": False,
        }


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    database_path: str
    created_at: str
    artifacts: tuple[dict[str, str | int], ...]
    master_key_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "database_path": self.database_path,
            "created_at": self.created_at,
            "artifacts": list(self.artifacts),
            "master_key": {"path": self.master_key_path, "moved": False},
        }


@dataclass(frozen=True, slots=True)
class ResetResult:
    archive_manifest: ArchiveManifest
    migration_revision: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, *, description: str) -> None:
    if not path.exists() or not path.is_file():
        raise ArchiveSafetyError(f"{description} is missing or is not a file")


def _require_master_key(path: Path) -> None:
    _require_regular_file(path, description="master.key")
    try:
        with path.open("rb") as handle:
            if not handle.read(1):
                raise ArchiveSafetyError("master.key is empty")
    except OSError as exc:
        raise ArchiveSafetyError("master.key is not readable") from exc


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ArchiveSafetyError("archive parent cannot be resolved")
        candidate = parent
    return candidate


def _artifact_paths(database_path: Path) -> tuple[tuple[ArtifactRole, Path], ...]:
    return (
        ("database", database_path),
        ("wal", Path(f"{database_path}-wal")),
        ("shm", Path(f"{database_path}-shm")),
    )


def plan_database_reset(
    *,
    database_path: Path,
    archive_root: Path | None = None,
    master_key_path: Path | None = None,
    now: datetime | None = None,
) -> ArchivePlan:
    database = Path(database_path).expanduser().resolve()
    _require_regular_file(database, description="database")
    key = Path(master_key_path or database.parent / "master.key").expanduser().resolve()
    _require_master_key(key)
    if key in {path.resolve() for _role, path in _artifact_paths(database)}:
        raise ArchiveSafetyError("master.key must be separate from the SQLite file set")
    archive_parent = Path(archive_root or database.parent / "archive").expanduser().resolve()
    existing_archive_parent = _existing_parent(archive_parent)
    if database.stat().st_dev != existing_archive_parent.stat().st_dev:
        raise ArchiveSafetyError("archive must be on the same filesystem as database")
    if shutil.disk_usage(existing_archive_parent).free < MINIMUM_FREE_BYTES:
        raise ArchiveSafetyError("insufficient free disk space for Incident Operations initialization")

    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    created_at = timestamp.isoformat().replace("+00:00", "Z")
    archive_directory = archive_parent / timestamp.strftime("pre-incident-operations-%Y%m%dT%H%M%SZ")
    if archive_directory.exists():
        raise ArchiveSafetyError("archive destination already exists")

    artifacts = tuple(
        ArchiveArtifactPlan(
            role=role,
            source=path,
            destination=archive_directory / path.name,
            size_bytes=path.stat().st_size,
        )
        for role, path in _artifact_paths(database)
        if path.exists() and path.is_file()
    )
    for role, path in _artifact_paths(database):
        if path.exists() and not path.is_file():
            raise ArchiveSafetyError(f"{role} artifact is not a regular file")
    if not artifacts or artifacts[0].role != "database":
        raise ArchiveSafetyError("database archive set is incomplete")
    return ArchivePlan(
        database_path=database,
        archive_directory=archive_directory,
        master_key_path=key,
        created_at=created_at,
        artifacts=artifacts,
    )


def _write_manifest(path: Path, manifest: ArchiveManifest) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            PRIVATE_FILE_MODE,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def archive_database(
    plan: ArchivePlan,
    *,
    confirm_process_stopped: bool,
) -> ArchiveManifest:
    if not confirm_process_stopped:
        raise ArchiveSafetyError("explicit confirmation that the process is stopped is required")
    _require_master_key(plan.master_key_path)
    current_artifacts = tuple(
        (role, path.resolve())
        for role, path in _artifact_paths(plan.database_path)
        if path.exists() and path.is_file()
    )
    planned_artifacts = tuple(
        (artifact.role, artifact.source.resolve()) for artifact in plan.artifacts
    )
    if current_artifacts != planned_artifacts:
        raise ArchiveSafetyError("database file set changed after preflight")
    for artifact in plan.artifacts:
        _require_regular_file(artifact.source, description=f"{artifact.role} artifact")
        if artifact.source.stat().st_size != artifact.size_bytes:
            raise ArchiveSafetyError("database file set changed after preflight")

    plan.archive_directory.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=PRIVATE_DIRECTORY_MODE,
    )
    plan.archive_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    os.chmod(plan.archive_directory, PRIVATE_DIRECTORY_MODE)
    moved: list[tuple[ArchiveArtifactPlan, int]] = []
    try:
        for artifact in plan.artifacts:
            original_mode = stat.S_IMODE(artifact.source.stat().st_mode)
            os.replace(artifact.source, artifact.destination)
            os.chmod(artifact.destination, PRIVATE_FILE_MODE)
            moved.append((artifact, original_mode))
        manifest_artifact_items: list[dict[str, str | int]] = []
        for artifact in plan.artifacts:
            manifest_artifact_items.append(
                {
                "role": artifact.role,
                "name": artifact.destination.name,
                "size_bytes": artifact.destination.stat().st_size,
                "sha256": _sha256(artifact.destination),
                }
            )
        manifest_artifacts = tuple(manifest_artifact_items)
        manifest = ArchiveManifest(
            database_path=str(plan.database_path),
            created_at=plan.created_at,
            artifacts=manifest_artifacts,
            master_key_path=str(plan.master_key_path),
        )
        _write_manifest(plan.archive_directory / "manifest.json", manifest)
        return manifest
    except Exception:
        for artifact, original_mode in reversed(moved):
            if artifact.destination.exists() and not artifact.source.exists():
                os.replace(artifact.destination, artifact.source)
                os.chmod(artifact.source, original_mode)
        if plan.archive_directory.exists() and not any(plan.archive_directory.iterdir()):
            plan.archive_directory.rmdir()
        raise


def _read_manifest(archive_directory: Path) -> ArchiveManifest:
    try:
        raw: Any = json.loads(
            (archive_directory / "manifest.json").read_text(encoding="utf-8")
        )
        if raw["schema_version"] != 1 or raw["master_key"]["moved"] is not False:
            raise ValueError
        artifacts = tuple(raw["artifacts"])
        manifest = ArchiveManifest(
            database_path=str(raw["database_path"]),
            created_at=str(raw["created_at"]),
            artifacts=artifacts,
            master_key_path=str(raw["master_key"]["path"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ArchiveSafetyError("archive manifest is invalid") from exc
    roles = [item.get("role") for item in manifest.artifacts]
    if not roles or roles[0] != "database" or len(roles) != len(set(roles)):
        raise ArchiveSafetyError("archive manifest artifact set is invalid")
    for item in manifest.artifacts:
        name = item.get("name")
        checksum = item.get("sha256")
        if not isinstance(name, str) or Path(name).name != name:
            raise ArchiveSafetyError("archive manifest artifact name is invalid")
        source = archive_directory / name
        _require_regular_file(source, description="archived artifact")
        if not isinstance(checksum, str) or _sha256(source) != checksum:
            raise ArchiveSafetyError("archived artifact checksum mismatch")
    return manifest


def restore_database(
    *,
    archive_directory: Path,
    database_path: Path,
    master_key_path: Path,
    confirm_process_stopped: bool,
) -> tuple[Path, ...]:
    if not confirm_process_stopped:
        raise ArchiveSafetyError("explicit confirmation that the process is stopped is required")
    archive = Path(archive_directory).expanduser().resolve()
    database = Path(database_path).expanduser().resolve()
    key = Path(master_key_path).expanduser().resolve()
    _require_master_key(key)
    manifest = _read_manifest(archive)
    if key != Path(manifest.master_key_path).expanduser().resolve():
        raise ArchiveSafetyError("restore master.key path does not match archive manifest")
    role_targets: dict[str, Path] = dict(_artifact_paths(database))
    targets: list[tuple[Path, Path]] = []
    for item in manifest.artifacts:
        role = item["role"]
        if not isinstance(role, str) or role not in role_targets:
            raise ArchiveSafetyError("archive manifest artifact role is invalid")
        target = role_targets[role]
        if target.exists():
            raise ArchiveSafetyError(f"restore target already exists: {target.name}")
        targets.append((archive / str(item["name"]), target))

    database.parent.mkdir(parents=True, exist_ok=True)
    temporary_files: list[Path] = []
    restored: list[Path] = []
    try:
        for source, target in targets:
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore")
            shutil.copy2(source, temporary)
            os.chmod(temporary, PRIVATE_FILE_MODE)
            temporary_files.append(temporary)
        for temporary, (_source, target) in zip(temporary_files, targets, strict=True):
            os.replace(temporary, target)
            restored.append(target)
        return tuple(restored)
    except Exception:
        for temporary in temporary_files:
            if temporary.exists():
                temporary.unlink()
        for target in restored:
            if target.exists():
                target.unlink()
        raise


def reset_database(
    plan: ArchivePlan,
    *,
    confirm_process_stopped: bool,
) -> ResetResult:
    manifest = archive_database(
        plan,
        confirm_process_stopped=confirm_process_stopped,
    )
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=plan.database_path))
    try:
        upgrade_database(engine)
        revision = current_revision(engine)
        if revision is None:
            raise RuntimeError("Incident Operations migration did not establish a revision")
        return ResetResult(archive_manifest=manifest, migration_revision=revision)
    except Exception:
        engine.dispose()
        for _role, path in _artifact_paths(plan.database_path):
            if path.exists() and path.is_file():
                path.unlink()
        raise
    finally:
        engine.dispose()
