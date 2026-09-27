"""Archived migration placeholders must not look like a populated registry.

Migration 5 creates ARCHIVED EventSource rows for pre-F20 config. Counting them
as "the registry is populated" was the R1 defect (archive/HISTORY.md §3.3).
F21 then removed the `.env` poll path entirely, so an archived-only registry now means
"nothing is configured" rather than "the legacy fallback is serving" -- these
tests pin both halves of that.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api import event_sources as event_sources_api
from app.api import health as health_api
from app.api import incidents as incidents_api
from app.db import get_session
from app.registry_models import EventSource, F20Model
from app.models import Incident
from app.services.event_sources import has_managed_event_source, list_event_sources
from app.services.source_scope import has_event_sources, visible_source_ids


@pytest.fixture
def f20_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add_source(session: Session, source_id: str, state: str, name: str) -> EventSource:
    now = datetime.now(timezone.utc)
    source = EventSource(
        id=source_id,
        type="ALERTMANAGER",
        name=name,
        lifecycle_state=state,
        created_at=now,
        updated_at=now,
        archived_at=now if state == "ARCHIVED" else None,
        version=1,
    )
    session.add(source)
    session.commit()
    return source


def migration_placeholders(session: Session) -> None:
    """Exactly what a migrated pre-F20 database looks like."""
    add_source(session, "legacy", "ARCHIVED", "Archived legacy source")
    add_source(session, "am:a17d2cdb", "ARCHIVED", "Archived Alertmanager a17d2cdb")


def test_archived_placeholders_do_not_populate_the_registry(f20_session) -> None:
    migration_placeholders(f20_session)

    assert has_managed_event_source(f20_session) is False
    assert has_event_sources(f20_session) is False


def test_enabled_source_does_populate_the_registry(f20_session) -> None:
    migration_placeholders(f20_session)
    add_source(f20_session, "src_live", "ENABLED", "Live AM")

    assert has_managed_event_source(f20_session) is True
    assert has_event_sources(f20_session) is True


def test_disabled_source_still_counts_as_managed(f20_session) -> None:
    """Disabled is a user choice; archived is a migration artifact."""
    add_source(f20_session, "src_off", "DISABLED", "Paused AM")

    assert has_managed_event_source(f20_session) is True


def test_listing_hides_archived_unless_asked(f20_session) -> None:
    migration_placeholders(f20_session)
    add_source(f20_session, "src_live", "ENABLED", "Live AM")

    default_ids = [item["id"] for item in list_event_sources(f20_session)]
    assert default_ids == ["src_live"]

    all_ids = sorted(
        item["id"] for item in list_event_sources(f20_session, include_archived=True)
    )
    assert all_ids == ["am:a17d2cdb", "legacy", "src_live"]


def test_archived_only_registry_makes_nothing_visible(f20_session) -> None:
    """F21 replaced the `.env` fallback with adoption at migration time.

    An archived-only registry is "not configured", not "serving from .env";
    historical rows are repointed by migration v6 instead of being surfaced
    through a second poll path. See ADR 0004.
    """
    migration_placeholders(f20_session)

    assert visible_source_ids(f20_session) == []
def _health_client(session: Session) -> TestClient:
    app = FastAPI()
    app.include_router(health_api.router, prefix="/api")
    app.include_router(incidents_api.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_health_reports_unconfigured_when_only_placeholders_exist(f20_session) -> None:
    migration_placeholders(f20_session)
    body = _health_client(f20_session).get("/api/health").json()

    assert body["source_mode"] == "UNCONFIGURED"
    assert body["configuration_status"] == "unconfigured"


def test_health_reports_registry_mode_with_a_managed_source(f20_session) -> None:
    migration_placeholders(f20_session)
    add_source(f20_session, "src_live", "ENABLED", "Live AM")
    body = _health_client(f20_session).get("/api/health").json()

    assert body["source_mode"] == "REGISTRY"
    assert body["configuration_status"] == "configured"


def test_health_reports_unconfigured_on_an_empty_registry(f20_session) -> None:
    """`.env` no longer participates: only the registry decides."""
    from app.config import settings as app_settings

    assert app_settings.alertmanager_url, "conftest keeps a URL in settings"

    body = _health_client(f20_session).get("/api/health").json()
    assert body["source_mode"] == "UNCONFIGURED"


def test_orphan_incidents_are_not_counted_until_adopted(f20_session) -> None:
    """Rows on a retired source id stay hidden; migration v6 repoints them."""
    from app.services.source_identity import active_source_id

    migration_placeholders(f20_session)
    now = datetime.now(timezone.utc)
    f20_session.add(
        Incident(
            group_key="source=legacy|rule=none",
            title="KubePodCrashLooping · qa",
            severity="critical",
            source_state="firing",
            source_id=active_source_id(),
            environment="prod",
            policy_version=1,
            grouping_explanation="legacy",
            created_at=now,
            updated_at=now,
        )
    )
    f20_session.commit()

    body = _health_client(f20_session).get("/api/health").json()
    assert body["incident_count"] == 0
    assert _health_client(f20_session).get("/api/incidents").json() == []

    # Adopting the row -- what migration v6 does -- makes it visible again.
    add_source(f20_session, active_source_id(), "ENABLED", "Adopted AM")
    body = _health_client(f20_session).get("/api/health").json()
    assert body["incident_count"] == 1
    listed = _health_client(f20_session).get("/api/incidents").json()
    assert [item["title"] for item in listed] == ["KubePodCrashLooping · qa"]
