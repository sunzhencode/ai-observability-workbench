"""The read side of the occurrence history (F26 / CAP-11).

The two properties worth pinning: paging must not drop or repeat a row while new
history is being appended at the head, and a record must stay readable after
everything it names has been deleted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.api import incident_occurrences
from app.db import get_session
from app.registry_models import EventSource, F20Model, IncidentOccurrence
from app.models import Incident

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


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
        session.add(EventSource(id="src_a", name="nonprod AM", lifecycle_state="ENABLED"))
        session.add(EventSource(id="src_b", name="staging AM", lifecycle_state="ENABLED"))
        session.flush()
        yield session


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(incident_occurrences.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _record(
    session: Session,
    *,
    incident_id: int,
    occurrence_no: int = 1,
    source_id: str = "src_a",
    minutes_ago: int = 0,
    conclusion: str = "NEW",
) -> IncidentOccurrence:
    record = IncidentOccurrence(
        incident_id=incident_id,
        occurrence_no=occurrence_no,
        source_id=source_id,
        source_name="nonprod AM" if source_id == "src_a" else "staging AM",
        group_key=f"gk-{incident_id}-{occurrence_no}",
        title="KubePersistentVolumeFillingUp",
        started_at=NOW - timedelta(minutes=minutes_ago + 60),
        recovered_at=NOW - timedelta(minutes=minutes_ago),
        member_count=2,
        member_max_severity="warning",
        handling_conclusion=conclusion,
    )
    session.add(record)
    session.flush()
    return record


class TestListing:
    def test_it_returns_newest_first(self, session, client) -> None:
        _record(session, incident_id=1, minutes_ago=30)
        _record(session, incident_id=2, minutes_ago=10)
        _record(session, incident_id=3, minutes_ago=20)

        body = client.get("/api/incident-occurrences").json()

        assert [item["incident_id"] for item in body["items"]] == [2, 3, 1]
        assert body["next_before_id"] is None

    def test_times_are_serialised_as_utc(self, session, client) -> None:
        """Otherwise the frontend (UTC+8) reads them as local and is 8h off."""

        _record(session, incident_id=1)

        item = client.get("/api/incident-occurrences").json()["items"][0]

        assert item["recovered_at"].endswith("Z")
        assert item["started_at"].endswith("Z")

    def test_it_filters_by_source(self, session, client) -> None:
        _record(session, incident_id=1, source_id="src_a")
        _record(session, incident_id=2, source_id="src_b")

        body = client.get("/api/incident-occurrences?source_ids=src_b").json()

        assert [item["source_id"] for item in body["items"]] == ["src_b"]

    def test_it_filters_by_conclusion(self, session, client) -> None:
        """The user's chosen way to slice history (D2 / D16)."""

        _record(session, incident_id=1, conclusion="NEW")
        _record(session, incident_id=2, conclusion="CLOSED")
        _record(session, incident_id=3, conclusion="FALSE_POSITIVE")

        closed = client.get("/api/incident-occurrences?conclusion=CLOSED").json()

        assert [item["incident_id"] for item in closed["items"]] == [2]

    def test_it_rejects_a_conclusion_that_is_not_a_handling_state(
        self, session, client
    ) -> None:
        assert client.get("/api/incident-occurrences?conclusion=nope").status_code == 422


class TestPaging:
    def test_the_cursor_walks_every_row_exactly_once(self, session, client) -> None:
        for index in range(7):
            _record(session, incident_id=index + 1, minutes_ago=index)

        seen: list[int] = []
        cursor: int | None = None
        for _ in range(5):
            url = "/api/incident-occurrences?limit=3"
            if cursor is not None:
                url += f"&before_id={cursor}"
            body = client.get(url).json()
            seen.extend(item["incident_id"] for item in body["items"])
            cursor = body["next_before_id"]
            if cursor is None:
                break

        assert cursor is None
        assert seen == [1, 2, 3, 4, 5, 6, 7]
        assert len(seen) == len(set(seen))

    def test_the_last_page_does_not_claim_another_one(self, session, client) -> None:
        """A `next_before_id` on an exactly-full last page sends the reader to
        an empty page and makes the list look broken."""

        for index in range(3):
            _record(session, incident_id=index + 1, minutes_ago=index)

        body = client.get("/api/incident-occurrences?limit=3").json()

        assert len(body["items"]) == 3
        assert body["next_before_id"] is None

    def test_history_appended_mid_walk_does_not_disturb_the_pages(
        self, session, client
    ) -> None:
        """Why the cursor is a keyset and not an offset.

        New records land at the *head* of this ordering. With `offset` the second
        page would shift by however many rows arrived and silently skip rows; a
        keyset cursor is anchored to a row the reader has already seen.
        """

        for index in range(4):
            _record(session, incident_id=index + 1, minutes_ago=index + 10)

        first = client.get("/api/incident-occurrences?limit=2").json()
        assert [item["incident_id"] for item in first["items"]] == [1, 2]

        # Two more occurrences end while the reader is on page one.
        _record(session, incident_id=99, minutes_ago=0)
        _record(session, incident_id=98, minutes_ago=1)

        second = client.get(
            f"/api/incident-occurrences?limit=2&before_id={first['next_before_id']}"
        ).json()

        assert [item["incident_id"] for item in second["items"]] == [3, 4]

    def test_the_page_size_is_bounded(self, session, client) -> None:
        assert client.get("/api/incident-occurrences?limit=201").status_code == 422
        assert client.get("/api/incident-occurrences?limit=0").status_code == 422


class TestItOutlivesWhatItNames:
    def test_a_record_survives_its_incident_being_deleted(
        self, session, client
    ) -> None:
        """D9: retention deletes the Incident long before the record expires."""

        session.add(
            Incident(
                id=1,
                source_id="src_a",
                group_key="gk-live",
                title="KubePersistentVolumeFillingUp",
            )
        )
        session.flush()
        _record(session, incident_id=1)

        incident = session.get(Incident, 1)
        session.delete(incident)
        session.flush()

        body = client.get("/api/incident-occurrences").json()

        assert len(body["items"]) == 1
        item = body["items"][0]
        # Still fully readable: nothing was joined at read time.
        assert item["source_name"] == "nonprod AM"
        assert item["title"] == "KubePersistentVolumeFillingUp"
        assert item["group_key"] == "gk-1-1"

    def test_a_record_of_a_removed_source_is_hidden_but_not_lost(
        self, session, client
    ) -> None:
        """Source visibility is shared with every other endpoint.

        Hidden from the default listing, still in the database -- the row is never
        deleted by a configuration change.
        """

        record = _record(session, incident_id=1, source_id="src_b")
        source = session.get(EventSource, "src_b")
        source.lifecycle_state = "ARCHIVED"
        session.add(source)
        session.flush()

        default = client.get("/api/incident-occurrences").json()
        assert [item["id"] for item in default["items"]] == []

        # Same semantic as every other endpoint: `include_archived` stops archived
        # sources being *removed*, it does not add them to an unrequested default
        # listing (which only ever contains ENABLED sources). Seeing an archived
        # source's history means asking for that source by id.
        archived = client.get(
            "/api/incident-occurrences?source_ids=src_b&include_archived=1"
        ).json()
        assert [item["id"] for item in archived["items"]] == [record.id]

        assert session.exec(select(IncidentOccurrence)).all() != []


class TestDetail:
    def test_it_returns_one_record(self, session, client) -> None:
        record = _record(session, incident_id=1)

        body = client.get(f"/api/incident-occurrences/{record.id}").json()

        assert body["id"] == record.id
        assert body["occurrence_no"] == 1
        assert body["member_max_severity"] == "warning"

    def test_an_unknown_id_is_a_readable_404(self, session, client) -> None:
        response = client.get("/api/incident-occurrences/4242")

        assert response.status_code == 404
        assert response.json()["detail"] == "Occurrence not found"
