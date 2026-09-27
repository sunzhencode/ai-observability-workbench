"""Migration v11: create the F27 tables, seed nothing, touch nothing existing.

The ledger's rules are the ones that bite here — an applied migration's checksum
covers the source of every class in `checksum_dependencies`, so these four models
are frozen from the moment this ships. A field added to any of them later makes
every existing database refuse to start.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app.migrations import MIGRATIONS, run_migrations

F27_TABLES = (
    "metricquerytemplate",
    "modelchannel",
    "modelchannelrevision",
    "investigation",
)


def _fresh(tmp_path: Path):
    path = tmp_path / "f27.db"
    engine = create_engine(f"sqlite:///{path}")
    return engine, path


def _at_v10(tmp_path: Path):
    """A database that stopped at v10, i.e. one that upgraded before F27."""

    engine, path = _fresh(tmp_path)
    run_migrations(engine, database_path=path)
    with engine.begin() as connection:
        for table in F27_TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
        connection.execute(text("DELETE FROM schemamigration WHERE version = 11"))
    return engine, path


def test_the_ledger_is_append_only_and_contains_v11() -> None:
    """`in`, not "is last": v12 already sits behind it and more will follow.

    Pinning "v11 is the latest" would turn every future migration into a failing
    test with nothing actually wrong — the property worth guarding is that the
    ledger only ever grows, in order, without duplicates.
    """

    versions = [migration.version for migration in MIGRATIONS]
    assert versions == sorted(versions), "migrations must stay in order"
    assert len(versions) == len(set(versions)), "duplicate migration version"
    assert 11 in versions


def test_upgrading_from_v10_creates_the_tables_and_backs_up_first(tmp_path: Path) -> None:
    engine, path = _at_v10(tmp_path)
    existing = set(inspect(engine).get_table_names())
    assert not existing & set(F27_TABLES)

    report = run_migrations(engine, database_path=path)

    assert 11 in report.applied_versions
    tables = set(inspect(engine).get_table_names())
    assert set(F27_TABLES) <= tables
    assert report.backup_path is not None and report.backup_path.exists()


def test_repeated_startups_apply_it_exactly_once(tmp_path: Path) -> None:
    engine, path = _at_v10(tmp_path)

    first = run_migrations(engine, database_path=path)
    assert 11 in first.applied_versions
    for _ in range(3):
        assert run_migrations(engine, database_path=path).applied_versions == []

    with engine.connect() as connection:
        applied = connection.execute(
            text("SELECT COUNT(*) FROM schemamigration WHERE version = 11")
        ).scalar_one()
    assert applied == 1


def test_it_seeds_nothing(tmp_path: Path) -> None:
    """Built-in templates are seeded at startup, not by the migration.

    Their PromQL has to keep changing as it meets real clusters, and an applied
    migration's checksum is frozen — data that must stay correctable cannot live
    inside one.
    """

    engine, path = _at_v10(tmp_path)
    report = run_migrations(engine, database_path=path)

    assert report.details[11] == {"seeded_templates": 0}
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM metricquerytemplate")
        ).scalar_one()
    assert count == 0


def test_it_leaves_existing_rows_untouched(tmp_path: Path) -> None:
    engine, path = _at_v10(tmp_path)
    with engine.begin() as connection:
        before = {
            name: connection.execute(
                text(f"SELECT COUNT(*) FROM {name}")  # noqa: S608 - table names are literals
            ).scalar_one()
            for name in ("eventsource", "incidentoccurrence", "alert", "incident")
        }

    run_migrations(engine, database_path=path)

    with engine.connect() as connection:
        after = {
            name: connection.execute(
                text(f"SELECT COUNT(*) FROM {name}")  # noqa: S608
            ).scalar_one()
            for name in before
        }
    assert after == before


def test_a_brand_new_database_reaches_the_same_schema(tmp_path: Path) -> None:
    """Fresh and upgraded databases must not diverge.

    Migration 5 calls `create_all` with no `tables=` argument, so on a brand-new
    database it already creates every table later added to this metadata — which
    makes v11's `checkfirst=True` a no-op there. Same destination, different
    route; this pins that the destination really is the same.
    """

    fresh_engine, fresh_path = _fresh(tmp_path / "a")
    (tmp_path / "a").mkdir()
    fresh_engine, fresh_path = _fresh(tmp_path / "a")
    run_migrations(fresh_engine, database_path=fresh_path)

    upgraded_engine, upgraded_path = _at_v10(tmp_path)
    run_migrations(upgraded_engine, database_path=upgraded_path)

    fresh_tables = set(inspect(fresh_engine).get_table_names())
    upgraded_tables = set(inspect(upgraded_engine).get_table_names())
    assert set(F27_TABLES) <= fresh_tables
    assert fresh_tables == upgraded_tables


def test_frozen_models_are_listed_in_the_checksum(tmp_path: Path) -> None:
    """Whoever adds a field to these must be stopped by an existing database."""

    from app.registry_models import (
        Investigation,
        MetricQueryTemplate,
        ModelChannel,
        ModelChannelRevision,
    )

    v11 = next(m for m in MIGRATIONS if m.version == 11)
    assert set(v11.checksum_dependencies) == {
        MetricQueryTemplate,
        ModelChannel,
        ModelChannelRevision,
        Investigation,
    }


def test_the_model_source_really_feeds_the_checksum() -> None:
    """The freeze has to be more than a comment saying it is frozen.

    Editing any of the four classes must move v11's checksum, which is what makes
    every existing database refuse to start and therefore what makes "add a new
    table instead of a field" enforceable. Proven by building the same migration
    without its dependencies: a different checksum means the class source is
    genuinely part of the input.
    """

    from app.migrations import Migration

    v11 = next(m for m in MIGRATIONS if m.version == 11)
    assert v11.checksum_dependencies, "v11 must freeze its models"

    without_models = Migration(
        version=v11.version,
        name=v11.name,
        signature=v11.signature,
        apply=v11.apply,
        transactional=v11.transactional,
    )
    assert without_models.checksum != v11.checksum


# --------------------------------------------------------------------------
# v12：模板的 priority / display_unit（D40 / D47）
# --------------------------------------------------------------------------


def _at_v11(tmp_path: Path):
    """A database that stopped at v11 — i.e. one that upgraded before the review."""

    engine, path = _fresh(tmp_path)
    run_migrations(engine, database_path=path)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS metrictemplateextras"))
        connection.execute(text("DELETE FROM schemamigration WHERE version = 12"))
    return engine, path


def test_v12_adds_a_new_table_and_never_touches_the_frozen_one(tmp_path: Path) -> None:
    """The whole reason v12 exists: v11 froze `MetricQueryTemplate`.

    Adding `priority` to that model would change migration 11's checksum and stop
    every existing database from starting, so the field goes somewhere new.
    """

    engine, path = _at_v11(tmp_path)
    before = set(inspect(engine).get_columns("metricquerytemplate")[0].keys())

    report = run_migrations(engine, database_path=path)

    assert 12 in report.applied_versions
    assert "metrictemplateextras" in inspect(engine).get_table_names()
    columns = {c["name"] for c in inspect(engine).get_columns("metricquerytemplate")}
    assert "priority" not in columns and "display_unit" not in columns
    assert before  # sanity: the frozen table still exists


def test_v12_backfills_nothing(tmp_path: Path) -> None:
    """No row means "the defaults" — exactly what every template had before."""

    engine, path = _at_v11(tmp_path)
    report = run_migrations(engine, database_path=path)
    assert report.details[12] == {"backfilled_extras": 0}


def test_v12_applies_exactly_once(tmp_path: Path) -> None:
    engine, path = _at_v11(tmp_path)
    assert 12 in run_migrations(engine, database_path=path).applied_versions
    for _ in range(3):
        assert run_migrations(engine, database_path=path).applied_versions == []
