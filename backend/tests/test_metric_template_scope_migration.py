"""Migration v15: template Source Scope without touching frozen v11/v12/v14."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.db import create_sqlite_engine
from app.migrations import MIGRATIONS, run_migrations
from app.registry_models import (
    EventSource,
    MetricQueryTemplate,
    MetricTemplateOrigin,
    MetricTemplateSourceScope,
)


TABLE = "metrictemplatesourcescope"


def _pre_v15(tmp_path: Path):
    import app.migrations as migration_module

    original = migration_module.MIGRATIONS
    migration_module.MIGRATIONS = tuple(item for item in original if item.version < 15)
    try:
        path = tmp_path / "pre-template-scope.db"
        engine = create_sqlite_engine(path)
        run_migrations(engine, database_path=path)
    finally:
        migration_module.MIGRATIONS = original
    return engine, path


def _seed_imported_and_handwritten(engine) -> tuple[int, int]:
    with Session(engine) as session:
        session.add(EventSource(id="src_a", name="A"))
        imported = MetricQueryTemplate(name="imported", promql="up")
        handwritten = MetricQueryTemplate(name="hand", promql="up")
        session.add(imported)
        session.add(handwritten)
        session.flush()
        session.add(
            MetricTemplateOrigin(
                template_id=int(imported.id or 0),
                source_id="src_a",
                dashboard_uid="d",
                panel_id=1,
                ref_id="A",
                imported_promql="up",
            )
        )
        session.commit()
        return int(imported.id or 0), int(handwritten.id or 0)


def test_v15_is_appended_without_changing_v14() -> None:
    versions = [item.version for item in MIGRATIONS]
    assert versions[-2:] == [14, 15]
    assert next(item for item in MIGRATIONS if item.version == 14).name == (
        "grafana_judgment_baselines"
    )


def test_upgrade_backfills_only_imported_templates_to_their_origin_source(
    tmp_path: Path,
) -> None:
    engine, path = _pre_v15(tmp_path)
    imported_id, handwritten_id = _seed_imported_and_handwritten(engine)
    # Migration 5 creates every class currently registered in the metadata even
    # for a synthetic old database. A real pre-v15 database does not have it.
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))

    report = run_migrations(engine, database_path=path)

    assert 15 in report.applied_versions
    assert report.details[15] == {"backfilled_template_scopes": 1}
    assert report.backup_path is not None
    assert ".pre-v15-" in report.backup_path.name
    with Session(engine) as session:
        rows = session.exec(select(MetricTemplateSourceScope)).all()
        assert len(rows) == 1
        assert rows[0].template_id == imported_id
        assert rows[0].scope_mode == "SELECTED"
        assert rows[0].source_ids_json == ["src_a"]
        assert all(row.template_id != handwritten_id for row in rows)


def test_fresh_and_repeated_startups_have_one_v15_ledger_entry(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    engine = create_sqlite_engine(path)
    first = run_migrations(engine, database_path=path)
    assert 15 in first.applied_versions
    assert TABLE in inspect(engine).get_table_names()

    for _ in range(3):
        assert run_migrations(engine, database_path=path).applied_versions == []
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM schemamigration WHERE version = 15")
        ).scalar_one() == 1


def test_deleting_a_template_cascades_its_scope(tmp_path: Path) -> None:
    path = tmp_path / "cascade.db"
    engine = create_sqlite_engine(path)
    run_migrations(engine, database_path=path)
    with Session(engine) as session:
        row = MetricQueryTemplate(name="scoped", promql="up")
        session.add(row)
        session.flush()
        template_id = int(row.id or 0)
        session.add(
            MetricTemplateSourceScope(
                template_id=template_id,
                scope_mode="SELECTED",
                source_ids_json=["src_a"],
            )
        )
        session.commit()

    with Session(engine) as session:
        session.delete(session.get(MetricQueryTemplate, template_id))
        session.commit()

    with Session(engine) as session:
        assert session.exec(select(MetricTemplateSourceScope)).all() == []
