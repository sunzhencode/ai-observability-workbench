"""Current Incident handling, history query and retention use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from app.domains.incidents.actors import (
    InteractiveOperatorActor,
    require_interactive_operator,
)
from app.domains.incidents.collaboration import (
    IncidentTaskDraft,
    IncidentTaskState,
)
from app.domains.incidents.models import IncidentLifecycleSnapshot
from app.domains.incidents.response import ResponseAction
from app.domains.sources.models import PollCompleteness


@dataclass(frozen=True, slots=True)
class HandlingAuditView:
    id: int
    incident_id: int
    actor: str
    from_state: str
    to_state: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IncidentMemberView:
    id: int
    source_id: str
    upstream_fingerprint: str
    alertname: str
    severity: str
    cluster: str
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime | None
    missing_since_at: datetime | None
    source_state: str
    origin: str
    evidence_completeness: str
    last_seen_at: datetime
    incident_id: int


@dataclass(frozen=True, slots=True)
class IncidentDetailView:
    id: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    severity: str
    source_state: str
    freshness_state: str
    handling_state: str
    handling_version: int
    occurrence_no: int
    occurrence_started_at: datetime
    updated_at: datetime
    member_count: int
    aggregation_rule_id: int | None
    aggregation_rule_name: str | None
    aggregation_status: Literal["matched", "unmatched", "missing_labels"]
    grouping_explanation: str
    aggregation_rule_version: int | None
    members: tuple[IncidentMemberView, ...]
    handling_history: tuple[HandlingAuditView, ...]


@dataclass(frozen=True, slots=True)
class OccurrenceView:
    id: int
    incident_id: int
    occurrence_no: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    aggregation_rule_id: int | None
    aggregation_rule_name: str | None
    started_at: datetime
    recovered_at: datetime
    member_count: int
    member_max_severity: str
    handling_conclusion: str


@dataclass(frozen=True, slots=True)
class OccurrencePage:
    items: tuple[OccurrenceView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class OperationalOccurrenceView:
    id: int
    incident_id: int
    occurrence_no: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    signal_state: str
    signal_severity: str
    response_state: str
    resolution_code: str | None
    duplicate_of_occurrence_id: int | None
    service_id: int | None
    service_name: str | None
    assignment_origin: str
    service_assignment_state: str
    member_count: int
    evidence_completeness: str
    detected_at: datetime | None
    source_started_at: datetime | None
    ack_sla_seconds: int
    ack_sla_due_at: datetime | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    ack_sla_state: str
    ack_sla_remaining_seconds: int | None
    noise_state: str
    noise_reason: str | None
    noise_scope: str | None
    noise_starts_at: datetime | None
    noise_ends_at: datetime | None
    noise_remaining_seconds: int | None
    latest_activity_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class OperationalOccurrencePage:
    items: tuple[OperationalOccurrenceView, ...]
    next_cursor: str | None
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class TimelineEntryView:
    id: int
    occurrence_id: int
    sequence: int
    actor_type: str
    event_type: str
    summary: str
    detail: dict[str, Any]
    request_id: str
    source_ip: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IncidentTaskView:
    id: int
    occurrence_id: int
    title: str
    description: str | None
    due_at: datetime | None
    runbook_link: str | None
    status: IncidentTaskState
    result: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SimilarHistoryMatchReasonView:
    kind: str
    field: str
    value: str
    points: int


@dataclass(frozen=True, slots=True)
class SimilarHistoryView:
    occurrence_id: int
    occurrence_no: int
    title: str
    score: int
    match_reasons: tuple[SimilarHistoryMatchReasonView, ...]
    resolution_code: str
    operator_conclusion: str | None
    task_outcome: str | None
    handling_duration_seconds: int
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class CollaborationCommandResult:
    task: IncidentTaskView | None
    timeline: TimelineEntryView
    replayed: bool


@dataclass(frozen=True, slots=True)
class ResponseCommandResult:
    occurrence_id: int
    previous_state: str
    current_state: str
    resolution_code: str | None
    version: int
    timeline: tuple[TimelineEntryView, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class ServiceAssignmentCommandResult:
    occurrence_id: int
    service_id: int
    service_name: str
    assignment_origin: str
    service_assignment_state: str
    version: int
    timeline: TimelineEntryView
    replayed: bool


@dataclass(frozen=True, slots=True)
class RetentionView:
    alerts_deleted: int
    audits_deleted: int
    incidents_deleted: int
    occurrences_deleted: int


class IncidentStore(Protocol):
    def get_incident(self, incident_id: int) -> IncidentDetailView: ...
    def change_handling(
        self,
        incident_id: int,
        *,
        target: str,
        reason: str,
        actor: str,
        expected_version: int,
        now: datetime,
    ) -> HandlingAuditView: ...
    def list_occurrences(
        self,
        *,
        source_ids: tuple[str, ...],
        conclusion: str | None,
        include_archived: bool,
        cursor: str | None,
        limit: int,
    ) -> OccurrencePage: ...
    def get_occurrence(self, occurrence_id: int, *, include_archived: bool) -> OccurrenceView: ...
    def list_operational_occurrences(
        self,
        *,
        view: str,
        source_ids: tuple[str, ...],
        signal_states: tuple[str, ...],
        cursor: str | None,
        limit: int,
        now: datetime,
    ) -> OperationalOccurrencePage: ...
    def get_operational_occurrence(
        self, occurrence_id: int, *, now: datetime
    ) -> OperationalOccurrenceView: ...
    def list_timeline(
        self, occurrence_id: int, *, after_sequence: int, limit: int
    ) -> tuple[TimelineEntryView, ...]: ...
    def list_tasks(self, occurrence_id: int) -> tuple[IncidentTaskView, ...]: ...
    def list_similar_history(
        self, occurrence_id: int, *, limit: int = 10
    ) -> tuple[SimilarHistoryView, ...]: ...
    def cleanup(self, *, now: datetime, runtime_days: int, history_days: int) -> RetentionView: ...


class IncidentResponsePort(Protocol):
    def execute_response_command(
        self,
        occurrence_id: int,
        *,
        action: ResponseAction,
        actor: InteractiveOperatorActor,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> ResponseCommandResult: ...


class IncidentServiceAssignmentPort(Protocol):
    def assign_service(
        self,
        occurrence_id: int,
        *,
        service_id: int,
        actor: InteractiveOperatorActor,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> ServiceAssignmentCommandResult: ...


class IncidentCollaborationPort(Protocol):
    def create_task(
        self,
        occurrence_id: int,
        *,
        draft: IncidentTaskDraft,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult: ...
    def transition_task(
        self,
        occurrence_id: int,
        task_id: int,
        *,
        expected_version: int,
        target: IncidentTaskState,
        result: str | None,
        reason: str | None,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult: ...
    def add_note(
        self,
        occurrence_id: int,
        *,
        text: str,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult: ...
    def redact_note(
        self,
        occurrence_id: int,
        sequence: int,
        *,
        reason: str,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult: ...


class SourceIncidentReconciler(Protocol):
    def snapshot(self, session: Any, source_id: str) -> dict[int, IncidentLifecycleSnapshot]: ...
    def reconcile_source_changes(
        self,
        session: Any,
        before: dict[int, IncidentLifecycleSnapshot],
        *,
        source_id: str,
        observed_at: datetime,
        completeness: PollCompleteness,
    ) -> tuple[object, ...]: ...
    def mark_source_stale(
        self,
        session: Any,
        source_id: str,
        *,
        observed_at: datetime,
    ) -> None: ...


class ChangeIncidentHandling:
    def __init__(self, store: IncidentStore) -> None:
        self._store = store

    def execute(
        self,
        incident_id: int,
        *,
        target: str,
        reason: str,
        actor: str,
        expected_version: int,
        now: datetime,
    ) -> HandlingAuditView:
        return self._store.change_handling(
            incident_id,
            target=target,
            reason=reason,
            actor=actor,
            expected_version=expected_version,
            now=now,
        )


class IncidentResponseCommands:
    """The only application capability that advances human response state."""

    def __init__(self, port: IncidentResponsePort) -> None:
        self._port = port

    def execute(
        self,
        occurrence_id: int,
        *,
        action: ResponseAction,
        actor: InteractiveOperatorActor,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> ResponseCommandResult:
        return self._port.execute_response_command(
            occurrence_id,
            action=action,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source_ip=source_ip,
            now=now,
        )


class IncidentServiceAssignmentCommands:
    """The only application capability for manual Service assignment."""

    def __init__(self, port: IncidentServiceAssignmentPort) -> None:
        self._port = port

    def assign(
        self,
        occurrence_id: int,
        *,
        service_id: int,
        actor: InteractiveOperatorActor,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> ServiceAssignmentCommandResult:
        require_interactive_operator(actor)
        return self._port.assign_service(
            occurrence_id,
            service_id=service_id,
            actor=actor,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source_ip=source_ip,
            now=now,
        )


class IncidentCollaborationCommands:
    """The only application capability for human Task and Note writes."""

    def __init__(self, port: IncidentCollaborationPort) -> None:
        self._port = port

    def create_task(
        self,
        occurrence_id: int,
        *,
        draft: IncidentTaskDraft,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        require_interactive_operator(actor)
        return self._port.create_task(
            occurrence_id,
            draft=draft,
            actor=actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source_ip=source_ip,
            now=now,
        )

    def transition_task(
        self,
        occurrence_id: int,
        task_id: int,
        *,
        expected_version: int,
        target: IncidentTaskState,
        result: str | None,
        reason: str | None,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        require_interactive_operator(actor)
        return self._port.transition_task(
            occurrence_id,
            task_id,
            expected_version=expected_version,
            target=target,
            result=result,
            reason=reason,
            actor=actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source_ip=source_ip,
            now=now,
        )

    def add_note(
        self,
        occurrence_id: int,
        *,
        text: str,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        require_interactive_operator(actor)
        return self._port.add_note(
            occurrence_id,
            text=text,
            actor=actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source_ip=source_ip,
            now=now,
        )

    def redact_note(
        self,
        occurrence_id: int,
        sequence: int,
        *,
        reason: str,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        require_interactive_operator(actor)
        return self._port.redact_note(
            occurrence_id,
            sequence,
            reason=reason,
            actor=actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            source_ip=source_ip,
            now=now,
        )
