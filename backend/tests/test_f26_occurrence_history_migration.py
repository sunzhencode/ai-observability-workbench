"""Migration v10: the append-only occurrence history table.

F26 exists because the repository had no history object at all. This file pins
the two things a real database depends on: the table appears on upgrade, and the
upgrade invents nothing (ADR 0008 / design D12 -- for a group already `recovered`
at upgrade time we do not know when it recovered, so a synthesised
`recovered_at` would be a fabricated observation).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.db import create_sqlite_engine
from app.registry_models import IncidentOccurrence
from app.migrations import MIGRATIONS, run_migrations


def _migrated(tmp_path: Path, name: str = "workbench.db"):
    path = tmp_path / name
    engine = create_sqlite_engine(path)
    return engine, path, run_migrations(engine, database_path=path)


def _pre_v10(tmp_path: Path, name: str = "pre-f26.db"):
    """A database migrated to v9 only -- the state v10 has to upgrade."""

    import app.migrations as migration_module

    original = migration_module.MIGRATIONS
    migration_module.MIGRATIONS = tuple(
        item for item in original if item.version < 10
    )
    try:
        path = tmp_path / name
        engine = create_sqlite_engine(path)
        run_migrations(engine, database_path=path)
    finally:
        migration_module.MIGRATIONS = original
    return engine, path


class TestMigrationV10:
    def test_it_is_appended_after_v9(self) -> None:
        versions = [item.version for item in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)
        assert versions.index(10) == versions.index(9) + 1

    def test_a_synthesised_pre_v10_database_already_has_the_table(
        self, tmp_path: Path
    ) -> None:
        """The D14 divergence, pinned so nobody mistakes it for a bug.

        `_pre_v10` builds a *fresh* database with v10 filtered out, and migration 5
        calls `F20Model.metadata.create_all(connection)` with **no `tables=`
        argument** -- so the table this migration is supposed to add already exists
        there. It means a synthesised "pre-v10" database is not the same artifact as
        a real one created before F26 shipped, which is why the upgrade test below
        has to drop the table first.
        """

        engine, _path = _pre_v10(tmp_path)

        assert "incidentoccurrence" in inspect(engine).get_table_names()

    def test_upgrading_a_real_pre_v10_database_creates_the_table_and_backs_up_first(
        self, tmp_path: Path
    ) -> None:
        engine, path = _pre_v10(tmp_path)
        # A database created before F26 existed has no such table; the synthesised
        # one does (see the test above), so drop it to get the real starting state.
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE incidentoccurrence"))
        assert "incidentoccurrence" not in inspect(engine).get_table_names()

        report = run_migrations(engine, database_path=path)

        # `in`, not `==`: a pre-v10 database catches up on every later migration
        # in the same wave, so the exact list grows with each one that ships.
        # What this test is about is v10 specifically.
        assert 10 in report.applied_versions
        assert "incidentoccurrence" in inspect(engine).get_table_names()
        # An upgrade wave must leave a restorable copy of the pre-upgrade file.
        assert report.backup_path is not None and report.backup_path.exists()

    def test_repeated_startups_apply_it_exactly_once(self, tmp_path: Path) -> None:
        engine, path = _pre_v10(tmp_path)

        first = run_migrations(engine, database_path=path)
        assert 10 in first.applied_versions  # see the note above on `in` vs `==`

        for _ in range(3):
            again = run_migrations(engine, database_path=path)
            assert again.applied_versions == []

        with engine.connect() as connection:
            applied = connection.execute(
                text("SELECT COUNT(*) FROM schemamigration WHERE version = 10")
            ).scalar_one()
        assert applied == 1

    def test_it_backfills_nothing(self, tmp_path: Path) -> None:
        """D12: the history starts empty rather than starting with fabrications."""

        engine, path = _pre_v10(tmp_path)
        report = run_migrations(engine, database_path=path)

        assert report.details[10] == {"backfilled_occurrences": 0}
        with Session(engine) as session:
            assert session.exec(select(IncidentOccurrence)).all() == []

    def test_a_fresh_database_has_the_table_too(self, tmp_path: Path) -> None:
        """Fresh and upgraded databases must agree on the schema.

        They reach it by different routes: migration 5 calls
        `F20Model.metadata.create_all(connection)` with no `tables=` argument, so
        on a brand-new database v5 already creates this table and v10's
        `checkfirst=True` is a no-op. That divergence is accepted (it started with
        v8's `SourceThanosConfig`); what must not diverge is the end state.
        """

        engine, _path, report = _migrated(tmp_path)

        assert 10 in report.applied_versions
        assert "incidentoccurrence" in inspect(engine).get_table_names()

    def test_one_seal_per_occurrence_is_enforced_by_the_database(
        self, tmp_path: Path
    ) -> None:
        """R2/R5 idempotency has a constraint behind it, not just a pre-check.

        A repeated poll, a restart mid-transaction or a replay must not be able to
        write a second row for the same occurrence.
        """

        engine, _path, _report = _migrated(tmp_path)

        def row() -> IncidentOccurrence:
            return IncidentOccurrence(
                incident_id=1,
                occurrence_no=1,
                source_id="src_1",
                group_key="gk",
                member_count=1,
                member_max_severity="warning",
                handling_conclusion="NEW",
            )

        with Session(engine) as session:
            session.add(row())
            session.commit()

        with Session(engine) as session:
            session.add(row())
            with pytest.raises(Exception):
                session.commit()

        with Session(engine) as session:
            assert len(session.exec(select(IncidentOccurrence)).all()) == 1

    def test_the_record_carries_no_foreign_keys(self, tmp_path: Path) -> None:
        """D9: these records outlive the Incident, source and rule they name.

        A foreign key would either block the retention that is supposed to delete
        those rows, or cascade the history away with them. The denormalised
        columns are what make the record readable afterwards.
        """

        engine, _path, _report = _migrated(tmp_path)

        assert inspect(engine).get_foreign_keys("incidentoccurrence") == []
        columns = {
            item["name"] for item in inspect(engine).get_columns("incidentoccurrence")
        }
        assert {
            "source_id",
            "source_name",
            "group_key",
            "title",
            "aggregation_rule_name",
        } <= columns

    def test_the_severity_column_is_not_called_a_peak(self) -> None:
        """D10: the value is the max among members at seal time, not a running peak.

        Pinning the name is the cheapest guard against the field quietly being
        read as something it is not.
        """

        columns = set(IncidentOccurrence.__table__.columns.keys())
        assert "member_max_severity" in columns
        assert "peak_severity" not in columns
