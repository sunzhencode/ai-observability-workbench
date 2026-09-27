"""Migration v14: the four tables behind Grafana import and reference baselines.

`MetricQueryTemplate` is frozen by v11 and `MetricTemplateExtras` by v12, so the
reference baseline, the import origin, the per-source Grafana address and the
delivery evidence snapshot all have to be new tables. This file pins what a real
database depends on: they appear on upgrade, all four land in one migration,
deleting a template takes its side rows with it, and the idempotency constraint
that stops a double-confirm from producing two templates is enforced by the
database rather than by a pre-check.

**The version is 14, not the 13 the design names.** v13 was taken by the model
call log while this capability was still on paper (design §14 ledger).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.db import create_sqlite_engine
from app.migrations import MIGRATIONS, run_migrations
from app.registry_models import (
    DeliveryEvidenceSnapshot,
    EventSource,
    MetricQueryTemplate,
    MetricTemplateBaseline,
    MetricTemplateOrigin,
    SourceGrafanaConfig,
)

NEW_TABLES = (
    "sourcegrafanaconfig",
    "metrictemplatebaseline",
    "metrictemplateorigin",
    "deliveryevidencesnapshot",
)


def _migrated(tmp_path: Path, name: str = "workbench.db"):
    path = tmp_path / name
    engine = create_sqlite_engine(path)
    return engine, path, run_migrations(engine, database_path=path)


def _seed_source(engine, source_id: str = "src_1") -> str:
    """Both `source_id` columns are real foreign keys, so the source must exist.

    Deliberately unlike the history tables, which carry none: a Grafana address
    and an import origin are meaningless once their source is gone, and neither
    is kept beyond the source's own lifetime.
    """

    with Session(engine) as session:
        session.add(EventSource(id=source_id, name=source_id))
        session.commit()
    return source_id


def _pre_v14(tmp_path: Path, name: str = "pre-grafana-import.db"):
    """A database migrated to v13 only -- the state v14 has to upgrade."""

    import app.migrations as migration_module

    original = migration_module.MIGRATIONS
    migration_module.MIGRATIONS = tuple(
        item for item in original if item.version < 14
    )
    try:
        path = tmp_path / name
        engine = create_sqlite_engine(path)
        run_migrations(engine, database_path=path)
    finally:
        migration_module.MIGRATIONS = original
    return engine, path


class TestMigrationV14:
    def test_it_is_appended_after_v13(self) -> None:
        versions = [item.version for item in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)
        assert versions.index(14) == versions.index(13) + 1

    def test_a_fresh_database_has_all_four_tables(self, tmp_path: Path) -> None:
        engine, _path, report = _migrated(tmp_path)

        assert 14 in report.applied_versions
        names = set(inspect(engine).get_table_names())
        assert set(NEW_TABLES) <= names

    def test_upgrading_a_real_pre_v14_database_creates_them_and_backs_up_first(
        self, tmp_path: Path
    ) -> None:
        """A database created before this capability shipped has none of these tables.

        The synthesised "pre-v14" one does, because migration 5 calls
        `create_all(connection)` with **no `tables=` argument** and so builds
        every table that has since been added to this MetaData. Drop them to
        reach the state a real upgrade starts from.
        """

        engine, path = _pre_v14(tmp_path)
        with engine.begin() as connection:
            for table in NEW_TABLES:
                connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        assert not set(NEW_TABLES) & set(inspect(engine).get_table_names())

        report = run_migrations(engine, database_path=path)

        assert 14 in report.applied_versions
        assert set(NEW_TABLES) <= set(inspect(engine).get_table_names())
        assert report.backup_path is not None and report.backup_path.exists()

    def test_the_upgrade_preserves_existing_rows(self, tmp_path: Path) -> None:
        """Additive only: an upgrade must not disturb what is already stored."""

        engine, path = _pre_v14(tmp_path)
        with engine.begin() as connection:
            for table in NEW_TABLES:
                connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        with Session(engine) as session:
            session.add(
                MetricQueryTemplate(name="kept", promql="up", builtin_key=None)
            )
            session.commit()

        run_migrations(engine, database_path=path)

        with Session(engine) as session:
            rows = session.exec(select(MetricQueryTemplate)).all()
        assert [row.name for row in rows] == ["kept"]

    def test_repeated_startups_apply_it_exactly_once(self, tmp_path: Path) -> None:
        engine, path = _pre_v14(tmp_path)

        first = run_migrations(engine, database_path=path)
        assert 14 in first.applied_versions

        for _ in range(3):
            again = run_migrations(engine, database_path=path)
            assert again.applied_versions == []

        with engine.connect() as connection:
            applied = connection.execute(
                text("SELECT COUNT(*) FROM schemamigration WHERE version = 14")
            ).scalar_one()
        assert applied == 1

    def test_the_upgrade_writes_a_pre_v14_backup(self, tmp_path: Path) -> None:
        engine, path = _pre_v14(tmp_path)

        report = run_migrations(engine, database_path=path)

        assert report.backup_path is not None
        assert ".pre-v14-" in report.backup_path.name

    def test_it_backfills_nothing(self, tmp_path: Path) -> None:
        engine, path = _pre_v14(tmp_path)

        report = run_migrations(engine, database_path=path)

        assert report.details[14] == {"backfilled_baselines": 0}

    def test_the_delivery_evidence_table_ships_with_the_other_three(
        self, tmp_path: Path
    ) -> None:
        """Review Q1: stage 3 only uses it, but waiting costs a path divergence.

        Creating it later would mean a fresh database has it (migration 5's
        argument-less `create_all` builds it) while an upgraded one waits for
        v15 -- the same fork v8's `SourceThanosConfig` already introduced once.
        Additive tables are free; a second shape of "current schema" is not.
        """

        engine, _path, _report = _migrated(tmp_path)

        assert "deliveryevidencesnapshot" in inspect(engine).get_table_names()

    def test_deleting_a_template_takes_its_baseline_and_origin_with_it(
        self, tmp_path: Path
    ) -> None:
        """§14 R-12. `db.py` turns on `foreign_keys`, so CASCADE is real here."""

        engine, _path, _report = _migrated(tmp_path)
        _seed_source(engine)

        with Session(engine) as session:
            template = MetricQueryTemplate(name="imported", promql="up")
            session.add(template)
            session.flush()
            template_id = int(template.id or 0)
            session.add(
                MetricTemplateBaseline(
                    template_id=template_id, value=500.0, direction="HIGH_IS_BAD"
                )
            )
            session.add(
                MetricTemplateOrigin(
                    template_id=template_id,
                    source_id="src_1",
                    dashboard_uid="abc",
                    dashboard_title="MySQL",
                    panel_id=2,
                    panel_title="Connections",
                    ref_id="A",
                    imported_promql="up",
                )
            )
            session.commit()

        with Session(engine) as session:
            session.delete(session.get(MetricQueryTemplate, template_id))
            session.commit()

        with Session(engine) as session:
            assert session.exec(select(MetricTemplateBaseline)).all() == []
            assert session.exec(select(MetricTemplateOrigin)).all() == []

    def test_one_origin_per_panel_target_is_enforced_by_the_database(
        self, tmp_path: Path
    ) -> None:
        """§14 R-6: confirming twice must collide, not produce a second template.

        A pre-check cannot cover two browser tabs confirming at the same moment.
        """

        engine, _path, _report = _migrated(tmp_path)
        _seed_source(engine)

        def origin(template_id: int) -> MetricTemplateOrigin:
            return MetricTemplateOrigin(
                template_id=template_id,
                source_id="src_1",
                dashboard_uid="abc",
                dashboard_title="MySQL",
                panel_id=2,
                panel_title="Connections",
                ref_id="A",
                imported_promql="up",
            )

        with Session(engine) as session:
            first = MetricQueryTemplate(name="one", promql="up")
            second = MetricQueryTemplate(name="two", promql="up")
            session.add(first)
            session.add(second)
            session.flush()
            first_id, second_id = int(first.id or 0), int(second.id or 0)
            session.add(origin(first_id))
            session.commit()

        with Session(engine) as session:
            session.add(origin(second_id))
            with pytest.raises(Exception):
                session.commit()

        with Session(engine) as session:
            assert len(session.exec(select(MetricTemplateOrigin)).all()) == 1

    def test_ref_id_is_not_nullable(self) -> None:
        """Review Q-补: SQLite treats NULLs as distinct inside a UNIQUE index.

        A nullable `ref_id` would let every single-target panel insert an
        unlimited number of origin rows -- the idempotency constraint above
        would still exist and would simply never fire.
        """

        column = MetricTemplateOrigin.__table__.columns["ref_id"]
        assert column.nullable is False

    def test_the_origin_stores_the_query_text_not_a_digest(self) -> None:
        """Review Q-补2: the conflict view has to show the imported original.

        A digest cannot be displayed, so re-import could never put "what was
        imported / what you changed it to / what Grafana has now" side by side.
        """

        columns = set(MetricTemplateOrigin.__table__.columns.keys())
        assert "imported_promql" in columns
        assert "imported_promql_sha" not in columns

    def test_the_grafana_config_keeps_its_secret_in_an_envelope(self) -> None:
        """Review Q5: same shape as `SourceThanosConfig`, not a bare ciphertext.

        Grafana is Bearer-or-anonymous (design §4), so there is deliberately no
        `auth_kind` and no `username`: a null envelope *is* "anonymous".
        """

        columns = set(SourceGrafanaConfig.__table__.columns.keys())
        assert "secret_envelope_json" in columns
        assert "token_ciphertext" not in columns
        assert "auth_kind" not in columns
        assert "username" not in columns

    def test_one_grafana_config_per_source(self, tmp_path: Path) -> None:
        engine, _path, _report = _migrated(tmp_path)
        _seed_source(engine)

        with Session(engine) as session:
            session.add(
                SourceGrafanaConfig(source_id="src_1", base_url="http://10.0.0.5:3000")
            )
            session.commit()

        with Session(engine) as session:
            session.add(
                SourceGrafanaConfig(source_id="src_1", base_url="http://10.0.0.6:3000")
            )
            with pytest.raises(Exception):
                session.commit()

    def test_one_evidence_snapshot_per_occurrence(self, tmp_path: Path) -> None:
        engine, _path, _report = _migrated(tmp_path)

        with Session(engine) as session:
            session.add(
                DeliveryEvidenceSnapshot(incident_id=1, occurrence_no=1, summary_json={})
            )
            session.commit()

        with Session(engine) as session:
            session.add(
                DeliveryEvidenceSnapshot(incident_id=1, occurrence_no=1, summary_json={})
            )
            with pytest.raises(Exception):
                session.commit()

    def test_the_evidence_snapshot_has_no_cross_metadata_foreign_key(
        self, tmp_path: Path
    ) -> None:
        """`Incident` lives on the other MetaData, so a real FK is impossible.

        Same precedent as `IncidentOccurrence.incident_id`: a jump hint, not
        identity.
        """

        engine, _path, _report = _migrated(tmp_path)

        assert inspect(engine).get_foreign_keys("deliveryevidencesnapshot") == []

    def test_the_frozen_template_models_gained_no_columns(self) -> None:
        """v11 and v12 froze these two. Everything new goes in a new table.

        Editing either class changes an applied migration's checksum and every
        existing database refuses to start -- this pins the columns so the
        temptation to "just add one field" fails here instead of on a user's
        machine.
        """

        from app.registry_models import MetricTemplateExtras

        assert set(MetricQueryTemplate.__table__.columns.keys()) == {
            "id",
            "name",
            "promql",
            "required_labels_json",
            "description",
            "builtin_key",
            "user_modified",
            "enabled",
            "created_at",
            "updated_at",
        }
        assert set(MetricTemplateExtras.__table__.columns.keys()) == {
            "id",
            "template_id",
            "priority",
            "display_unit",
            "created_at",
            "updated_at",
        }
