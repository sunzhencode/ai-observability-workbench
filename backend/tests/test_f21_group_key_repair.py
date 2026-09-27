"""F21 regression: adoption repointed source_id but left group_key stale.

v6 moved historical rows onto the adopted source. The group key embeds the
source id, so every startup recomputed a key that matched nothing, emptied the
old incidents and tried to delete them -- which failed on the
`superseded_by_incident_id` foreign key and killed the application during
`init_db`, before it could serve a single request.

Two fixes, pinned separately: v7 recomputes the keys (reusing migration 5's
collision logic), and _regroup_all refuses to delete an incident that something
still points at.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.db import create_sqlite_engine, init_db
from app.migrations import run_migrations
from app.models import Alert, Incident
from app.services.aggregation_rules import regroup_all_with_aggregation_rules
from test_f20_migrations import _create_v4_schema, _insert_incident, _insert_profile

ADOPTED = "src_684cc4933d5b68a274016d15"


def rows(engine, sql: str) -> list[tuple]:
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text(sql)).all()]


def scalar(engine, sql: str):
    return rows(engine, sql)[0][0]


def post_v6_shape(path: Path):
    """A database in exactly the state v6 left the reporter's in.

    Every row already repointed onto the adopted source, group keys still
    naming the retired sources, and a superseded chain from migration 5.
    """
    _create_v4_schema(path)
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        _insert_profile(connection, profile_id=1, source_id="am:a17d2cdb", state="RETIRED")
        for incident_id, source_id in ((101, "am:a17d2cdb"), (102, "legacy")):
            _insert_incident(
                connection,
                incident_id=incident_id,
                source_id=source_id,
                environment="prod",
                source_state="firing",
                handling_state="NEW",
                updated_at="2026-07-20 00:00:00",
            )
    run_migrations(engine, database_path=path)
    # The synthetic v4 schema builds a partial notificationroute; a real
    # database gets the full one from migrations 2-4. Rebuild it from the ORM so
    # regroup can query it the way it does in production.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS notificationroute"))
    SQLModel.metadata.tables["notificationroute"].create(engine, checkfirst=True)
    return engine


def test_group_keys_match_the_source_they_point_at(tmp_path: Path) -> None:
    path = tmp_path / "repaired.db"
    engine = post_v6_shape(path)

    stale = rows(
        engine,
        "SELECT id FROM incident WHERE superseded_by_incident_id IS NULL "
        "AND group_key NOT LIKE 'source=' || source_id || '%'",
    )
    assert stale == [], "a canonical incident's key must name its own source"


def test_startup_is_stable_across_restarts(tmp_path: Path) -> None:
    """The user-visible symptom was a crash; the silent one was churn."""
    path = tmp_path / "stable.db"
    engine = post_v6_shape(path)

    def snapshot() -> tuple:
        with Session(engine) as session:
            regroup_all_with_aggregation_rules(session)
        return (
            tuple(rows(engine, "SELECT id, group_key FROM incident ORDER BY id")),
            scalar(engine, "SELECT count(*) FROM alert"),
        )

    first = snapshot()
    assert first == snapshot() == snapshot(), "regroup must reach a fixed point"


def test_no_alert_is_lost_by_the_repair(tmp_path: Path) -> None:
    path = tmp_path / "nolost.db"
    engine = post_v6_shape(path)
    before = scalar(engine, "SELECT count(*) FROM alert")

    with Session(engine) as session:
        regroup_all_with_aggregation_rules(session)

    assert scalar(engine, "SELECT count(*) FROM alert") == before
    orphans = scalar(
        engine,
        "SELECT count(*) FROM alert WHERE incident_id IS NOT NULL AND incident_id "
        "NOT IN (SELECT id FROM incident)",
    )
    assert orphans == 0


def test_superseded_chain_survives(tmp_path: Path) -> None:
    path = tmp_path / "chain.db"
    engine = post_v6_shape(path)

    dangling = scalar(
        engine,
        "SELECT count(*) FROM incident WHERE superseded_by_incident_id IS NOT NULL "
        "AND superseded_by_incident_id NOT IN (SELECT id FROM incident)",
    )
    assert dangling == 0, "a superseded incident must still point at a real one"


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_regroup_never_deletes_a_superseded_target(session) -> None:
    """The foreign key that took startup down, pinned directly."""
    now = datetime.now(timezone.utc)
    target = Incident(
        group_key="source=src_x|rule=1|cluster=a",
        title="target",
        severity="warning",
        source_state="firing",
        source_id="src_x",
        environment="prod",
        policy_version=1,
        grouping_explanation="t",
        created_at=now,
        updated_at=now,
    )
    session.add(target)
    session.commit()
    session.refresh(target)

    superseded = Incident(
        group_key=f"superseded={target.id}|abc",
        title="superseded",
        severity="warning",
        source_state="recovered",
        source_id="src_x",
        environment="prod",
        policy_version=1,
        grouping_explanation="s",
        superseded_by_incident_id=target.id,
        created_at=now,
        updated_at=now,
    )
    session.add(superseded)
    session.commit()

    # The target has no alerts, so the old code would have deleted it.
    assert session.exec(
        select(Alert.id).where(Alert.incident_id == target.id)
    ).first() is None

    regroup_all_with_aggregation_rules(session)

    assert session.get(Incident, target.id) is not None, (
        "deleting it violates superseded_by_incident_id and kills startup"
    )


def test_init_db_completes_on_the_reporter_shape(tmp_path: Path, monkeypatch) -> None:
    """End to end: the exact path that raised during application startup."""
    path = tmp_path / "startup.db"
    post_v6_shape(path)

    from app.config import settings
    import app.db as db_module

    monkeypatch.setattr(settings, "database_path", str(path))
    monkeypatch.setattr(db_module, "_engine", None)

    db_module.init_db()  # must not raise

    engine = create_sqlite_engine(path)
    assert scalar(engine, "SELECT count(*) FROM incident") > 0


def test_each_upgrade_wave_gets_its_own_backup(tmp_path: Path) -> None:
    """A later migration must not run behind an older wave's backup.

    The label used to be one of two fixed buckets, so once a pre-f20 backup
    existed every subsequent upgrade reused it and ran with no fresh copy --
    which is how v6 and v7 reached a real database unprotected.
    """
    path = tmp_path / "backups.db"
    post_v6_shape(path)  # runs the full ledger, creating the first backups

    made = sorted(item.name for item in tmp_path.glob("backups.db.pre-*.bak"))
    labels = {name.split(".pre-")[1].split("-")[0] for name in made}

    assert labels, "an upgrade from a non-fresh database must leave a backup"
    # v5 and v6/v7 are separate waves and must not share one file.
    assert len(made) == len(labels), f"one backup per label expected, got {made}"
