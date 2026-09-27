"""Seal an ended occurrence into the append-only history (F26 / CAP-11).

Deterministic and offline: it receives the change list the poll already computed
plus a session, and only ``flush``es -- the poll boundary owns the commit, so a
record can never survive a poll that rolled back.

**No second diff.** ``reconcile_incident_changes`` already compares the pre-poll
snapshot against the current rows, and `_meaningful_change` includes
``source_state``, so no ``-> recovered`` transition can escape it. It even
returns the list; ``ingest_alerts`` simply threw it away. History consumes that
same list, which is why history and notifications can never disagree about
whether an occurrence ended -- a second detection path is exactly the seam the
F21 R1 defect grew in (ADR 0004).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.registry_models import IncidentOccurrence
from app.models import Alert, AggregationRule, Incident
from app.services.grouping import max_severity
from app.services.notification_planner import IncidentChange
from app.services.source_scope import source_name_map


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _history_available(session: Session) -> bool:
    """Whether this database has the history table at all.

    It lives on `F20Model.metadata` while Alert/Incident live on
    `SQLModel.metadata`, and this repository deliberately exercises the
    deterministic core against sessions that only carry the latter -- that is the
    same shape as a pre-F20 database, and the reason `visible_source_ids` has a
    "no eventsource table" branch. History is additive, so a session without the
    table must lose history rather than break ingest.

    `inspect(session.connection())`, never `inspect(engine)`: the engine form
    checks out a second connection from the pool, which under `StaticPool` is the
    one this session is already using, and the following rollback then discards
    whatever the caller had just flushed.
    """
    return inspect(session.connection()).has_table("incidentoccurrence")


def _is_ending(change: IncidentChange, *, reconcile_lifecycle: bool) -> bool:
    """The four conditions from design D8. Each one is a way to get this wrong.

    * ``reconcile_lifecycle`` is the F20 completeness gate: only a poll where
      every enabled endpoint answered may read absence as recovery. A PARTIAL
      poll that simply did not see the alerts must not end anything.
    * ``LIVE_POLL`` excludes Thanos backfill and rule regroup -- neither observes
      anything, they rearrange what is already known.
    * ``previous_source_state is None`` means the pre-poll snapshot had no such
      Incident, so this row was created during this very poll. A brand-new
      Incident that is already ``recovered`` is not an occurrence that ran and
      ended; it is the memberless case below, or backfilled history.
    * The transition has to be *into* ``recovered`` from something else. Staying
      recovered is not an ending, and neither is going to ``pending_resolution``.
    """
    return (
        reconcile_lifecycle
        and change.origin == "LIVE_POLL"
        and change.previous_source_state is not None
        and change.previous_source_state != "recovered"
        and change.current_source_state == "recovered"
    )


def seal_ended_occurrences(
    session: Session,
    changes: list[IncidentChange],
    *,
    reconcile_lifecycle: bool,
    observed_at: datetime,
) -> int:
    """Write one immutable record per occurrence that ended in this poll."""

    endings = [
        change
        for change in changes
        if _is_ending(change, reconcile_lifecycle=reconcile_lifecycle)
    ]
    if not endings or not _history_available(session):
        return 0

    names = source_name_map(session)
    rules = {
        rule.id: rule.name
        for rule in session.exec(select(AggregationRule)).all()
        if rule.id is not None
    }
    sealed = 0
    seen: set[tuple[int, int]] = set()

    for change in endings:
        key = (change.incident_id, change.occurrence_no)
        # A duplicate inside one batch would hit the unique constraint at flush
        # time and take the whole poll transaction down with it.
        if key in seen:
            continue
        seen.add(key)

        incident = session.get(Incident, change.incident_id)
        if incident is None:
            continue

        members = session.exec(
            select(Alert).where(Alert.incident_id == incident.id)
        ).all()
        # The trap this guard exists for: `recompute_incident_lifecycle` derives
        # `recovered` for an Incident with zero members, so retention deleting the
        # members -- or a regroup moving them elsewhere -- looks exactly like a
        # recovery. Counting at seal time is the only way to tell the difference,
        # and a fabricated occurrence is indistinguishable from a real one once
        # written.
        if not members:
            continue

        if _existing(session, change.incident_id, change.occurrence_no) is not None:
            continue

        session.add(
            IncidentOccurrence(
                incident_id=incident.id or change.incident_id,
                occurrence_no=change.occurrence_no,
                source_id=incident.source_id,
                # Denormalised on purpose: this row outlives the source, the rule
                # and the Incident itself (design D9).
                source_name=names.get(incident.source_id, incident.source_id),
                group_key=incident.group_key,
                title=incident.title,
                aggregation_rule_id=incident.aggregation_rule_id,
                aggregation_rule_name=rules.get(incident.aggregation_rule_id or -1),
                started_at=_utc(incident.occurrence_started_at),
                recovered_at=_utc(observed_at),
                member_count=len(members),
                # Not a running peak -- see the column's own note and design D10.
                member_max_severity=max_severity(
                    [alert.severity for alert in members]
                ),
                handling_conclusion=incident.handling_state,
            )
        )
        sealed += 1

    session.flush()
    return sealed


def _existing(
    session: Session, incident_id: int, occurrence_no: int
) -> IncidentOccurrence | None:
    return session.exec(
        select(IncidentOccurrence).where(
            IncidentOccurrence.incident_id == incident_id,
            IncidentOccurrence.occurrence_no == occurrence_no,
        )
    ).first()


def sync_open_conclusion(session: Session, incident: Incident) -> bool:
    """Copy the human's verdict onto the record of the *current* occurrence.

    The one mutable field, and the reason is not convenience: upstream recovery
    never settles handling (CAP-04.9), so at seal time the conclusion is almost
    always ``NEW``. Freezing it there would make "filter history by conclusion"
    -- the criterion the user asked to slice history by -- answer "unsettled" for
    every record forever.

    The mutable window is *derived*, not stored: only the record whose
    ``occurrence_no`` still matches the Incident's can be updated. Once the group
    fires again the counter advances and that record is closed permanently; once
    the Incident is deleted nothing can reach it at all. The facts of the
    occurrence are never touched, and no record can be deleted or moved out of
    history -- which is the property ADR 0008 exists to protect.
    """

    if incident.id is None or not _history_available(session):
        return False
    record = _existing(session, incident.id, incident.occurrence_no)
    if record is None or record.handling_conclusion == incident.handling_state:
        return False
    record.handling_conclusion = incident.handling_state
    session.add(record)
    session.flush()
    return True
