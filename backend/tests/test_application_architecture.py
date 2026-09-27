"""Dependency, migration and enqueue-only scheduling gates."""

from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import inspect

from app.application.jobs import EnqueueOnlyScheduler, EnqueueOnlySchedulerState, JobQueue
from app.domains.operations.jobs import JobPool, JobSpec, JobState
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database
from app.adapters.persistence.jobs import SqlAlchemyJobStore

APP = Path(__file__).resolve().parents[1] / "app"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_new_layers_follow_api_adapter_to_application_to_domain_direction() -> None:
    violations: list[str] = []
    policies = {
        APP / "domains": ("fastapi", "sqlalchemy", "app.application", "app.adapters", "app.api"),
        APP / "application": (
            "fastapi",
            "sqlalchemy",
            "app.api",
            "app.adapters",
            "app.services",
            "app.sources",
        ),
        APP / "adapters": ("fastapi", "app.api", "app.services", "app.sources"),
    }
    for directory, forbidden in policies.items():
        for path in sorted(directory.rglob("*.py")):
            for name in _imports(path):
                if name.startswith(forbidden):
                    violations.append(f"{path.relative_to(APP)}:{name}")

    assert not violations, "platform dependency direction violated:\n" + "\n".join(violations)


def test_job_foundation_migration_adds_only_platform_job_and_event_tables(tmp_path: Path) -> None:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))
    upgrade_database(engine, revision="platform_0002")

    tables = set(inspect(engine).get_table_names())

    assert tables == {"alembic_version", "platform_event", "platform_job"}


async def test_scheduler_only_enqueues_durable_work(tmp_path: Path) -> None:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))
    upgrade_database(engine)
    queue = JobQueue(SqlAlchemyJobStore(create_session_factory(engine)))
    state = EnqueueOnlySchedulerState()
    scheduler = EnqueueOnlyScheduler(queue, state)
    await scheduler.start()

    job = scheduler.enqueue(
        JobSpec(
            kind="source.collect",
            pool=JobPool.SOURCE,
            subject_type="source",
            subject_id="source-a",
            payload={},
            payload_revision=1,
            idempotency_key="scheduler-request-0001",
        )
    )

    assert job.state is JobState.PENDING
    assert queue.count() == 1
