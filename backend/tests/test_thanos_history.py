"""History addresses are read from the source that owns them."""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.crypto import SecretBox
from app.registry_models import EventSource, F20Model, SourceThanosConfig
from app.services.thanos_history import enabled_thanos_connections

BOX = SecretBox("thanos-history-test-key")


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add_source(
    session,
    source_id: str,
    *,
    state: str = "ENABLED",
    url: str | None = "https://thanos.invalid",
    auth: str = "NONE",
    envelope=None,
) -> None:
    session.add(
        EventSource(id=source_id, name=source_id, lifecycle_state=state)
    )
    if url is not None:
        session.add(
            SourceThanosConfig(
                source_id=source_id,
                canonical_url=url,
                auth_kind=auth,
                secret_envelope_json=envelope,
            )
        )
    session.flush()


def test_only_enabled_sources_with_an_address_contribute(session) -> None:
    add_source(session, "src_on")
    add_source(session, "src_off", state="DISABLED")
    add_source(session, "src_no_history", url=None)
    add_source(session, "src_blank", url="")

    connections = enabled_thanos_connections(session, box=BOX)

    assert [item.source_id for item in connections] == ["src_on"]
    assert connections[0].usable is True


def test_a_token_is_decrypted_for_the_job_boundary(session) -> None:
    add_source(
        session,
        "src_token",
        auth="BEARER",
        envelope=BOX.encrypt("thanos-token"),
    )

    connection = enabled_thanos_connections(session, box=BOX)[0]

    assert connection.token == "thanos-token"
    assert "thanos-token" not in repr(connection)


def test_an_unreadable_secret_degrades_visibly_instead_of_disappearing(
    session,
) -> None:
    add_source(
        session,
        "src_bad_key",
        auth="BEARER",
        envelope=SecretBox("a-different-key").encrypt("thanos-token"),
    )

    connection = enabled_thanos_connections(session, box=BOX)[0]

    assert connection.error_code == "SECRET_UNAVAILABLE"
    assert connection.usable is False


def test_a_database_without_the_table_reports_no_history(session) -> None:
    session.connection().exec_driver_sql("DROP TABLE sourcethanosconfig")

    assert enabled_thanos_connections(session, box=BOX) == []
