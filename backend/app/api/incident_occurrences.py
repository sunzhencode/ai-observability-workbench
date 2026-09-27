"""Read-only endpoints for the occurrence history (F26 / CAP-11).

Read-only on purpose (ADR 0008 invariant 7): the history answers "what happened",
and changing a state belongs on the alerts page. Adding a write action here would
turn an append-only record into a mutable one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlmodel import Session, select

from app.db import get_session
from app.registry_models import IncidentOccurrence
from app.schemas import (
    HandlingStateValue,
    IncidentOccurrenceOut,
    IncidentOccurrencePage,
)
from app.services.source_scope import f20_table_available, visible_source_ids

router = APIRouter()

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _out(record: IncidentOccurrence) -> IncidentOccurrenceOut:
    return IncidentOccurrenceOut.model_validate(record.model_dump())


@router.get("/incident-occurrences", response_model=IncidentOccurrencePage)
def list_incident_occurrences(
    session: Session = Depends(get_session),
    source_ids: list[str] | None = Query(
        default=None, description="Comma separated or repeated source IDs"
    ),
    conclusion: HandlingStateValue | None = Query(
        default=None, description="Filter by the human's verdict on the occurrence"
    ),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    before_id: int | None = Query(
        default=None, ge=1, description="Last id of the previous page"
    ),
    include_archived: bool = Query(default=False),
) -> IncidentOccurrencePage:
    # A database that predates the table has no history rather than an error: the
    # page then shows its "history starts accumulating after this upgrade" state.
    if not f20_table_available(session, "incidentoccurrence"):
        return IncidentOccurrencePage(items=[])

    visible_ids = visible_source_ids(
        session, requested=source_ids, include_archived=include_archived
    )
    if not visible_ids:
        return IncidentOccurrencePage(items=[])

    statement = (
        select(IncidentOccurrence)
        .where(IncidentOccurrence.source_id.in_(visible_ids))
        .order_by(IncidentOccurrence.recovered_at.desc(), IncidentOccurrence.id.desc())
    )
    if conclusion is not None:
        statement = statement.where(
            IncidentOccurrence.handling_conclusion == conclusion
        )
    if before_id is not None:
        # Keyset, not offset: new records land at the *head* of this ordering, so an
        # offset would shift under a reader who is mid-walk and silently skip rows.
        #
        # The cursor stays a single id in the API, but the comparison is on the full
        # sort key `(recovered_at, id)` of the row it names. Design D13 argued the id
        # alone was enough because the table is appended in recovery order -- true in
        # production, and still too fragile to lean on: the very first test written
        # against it violated the assumption by accident and paging silently walked
        # backwards. Resolving the cursor row costs one tiny lookup and makes the
        # order the query's own business.
        cursor = session.get(IncidentOccurrence, before_id)
        if cursor is None:
            return IncidentOccurrencePage(items=[])
        statement = statement.where(
            or_(
                IncidentOccurrence.recovered_at < cursor.recovered_at,
                and_(
                    IncidentOccurrence.recovered_at == cursor.recovered_at,
                    IncidentOccurrence.id < cursor.id,
                ),
            )
        )

    # One extra row tells us whether another page exists without a second count
    # query -- and without claiming there is a next page when there is not.
    rows = list(session.exec(statement.limit(limit + 1)).all())
    has_more = len(rows) > limit
    page = rows[:limit]
    return IncidentOccurrencePage(
        items=[_out(item) for item in page],
        next_before_id=page[-1].id if has_more and page else None,
    )


@router.get(
    "/incident-occurrences/{occurrence_id}", response_model=IncidentOccurrenceOut
)
def get_incident_occurrence(
    occurrence_id: int,
    session: Session = Depends(get_session),
    include_archived: bool = Query(default=False),
) -> IncidentOccurrenceOut:
    if not f20_table_available(session, "incidentoccurrence"):
        raise HTTPException(status_code=404, detail="Occurrence not found")
    record = session.get(IncidentOccurrence, occurrence_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    visible_ids = visible_source_ids(
        session, requested=[record.source_id], include_archived=include_archived
    )
    if record.source_id not in visible_ids:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    return _out(record)
