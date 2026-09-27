"""Deterministic noise configuration views and interactive commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domains.incidents.actors import InteractiveOperatorActor, require_interactive_operator


@dataclass(frozen=True, slots=True)
class SourceNoiseControlsView:
    source_id: str
    flapping_enabled: bool
    storm_enabled: bool
    storm_alert_threshold: int
    storm_occurrence_threshold: int
    storm_active: bool
    storm_started_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class MaintenanceDraft:
    scope_kind: str
    source_id: str | None
    service_id: int | None
    aggregation_rule_id: int | None
    starts_at: datetime
    ends_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class MaintenanceView:
    id: int
    scope_kind: str
    source_id: str | None
    service_id: int | None
    aggregation_rule_id: int | None
    starts_at: datetime
    ends_at: datetime
    reason: str
    status: str
    ended_at: datetime | None
    version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SuppressionView:
    id: int
    occurrence_id: int
    starts_at: datetime
    ends_at: datetime
    reason: str
    status: str
    ended_at: datetime | None
    version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OccurrenceNoiseView:
    state: str
    reason: str | None
    scope: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    suppression_id: int | None = None
    suppression_version: int | None = None


class NoisePort(Protocol):
    def get_source_controls(self, source_id: str) -> SourceNoiseControlsView: ...
    def update_source_controls(
        self,
        source_id: str,
        *,
        flapping_enabled: bool,
        storm_enabled: bool,
        storm_alert_threshold: int,
        storm_occurrence_threshold: int,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SourceNoiseControlsView: ...
    def list_maintenance(self, *, include_ended: bool) -> tuple[MaintenanceView, ...]: ...
    def create_maintenance(
        self, draft: MaintenanceDraft, *, actor: InteractiveOperatorActor, now: datetime
    ) -> MaintenanceView: ...
    def end_maintenance(
        self,
        maintenance_id: int,
        *,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> MaintenanceView: ...
    def create_suppression(
        self,
        occurrence_id: int,
        *,
        duration_seconds: int,
        reason: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SuppressionView: ...
    def end_suppression(
        self,
        occurrence_id: int,
        *,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SuppressionView: ...
    def occurrence_noise(self, occurrence_id: int, *, now: datetime) -> OccurrenceNoiseView: ...


class NoiseCommands:
    def __init__(self, port: NoisePort) -> None:
        self._port = port

    @staticmethod
    def _actor(actor: InteractiveOperatorActor) -> InteractiveOperatorActor:
        require_interactive_operator(actor)
        return actor

    def create_maintenance(
        self, draft: MaintenanceDraft, *, actor: InteractiveOperatorActor, now: datetime
    ) -> MaintenanceView:
        return self._port.create_maintenance(draft, actor=self._actor(actor), now=now)

    def end_maintenance(
        self,
        maintenance_id: int,
        *,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> MaintenanceView:
        return self._port.end_maintenance(
            maintenance_id,
            expected_version=expected_version,
            actor=self._actor(actor),
            now=now,
        )

    def create_suppression(
        self,
        occurrence_id: int,
        *,
        duration_seconds: int,
        reason: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SuppressionView:
        return self._port.create_suppression(
            occurrence_id,
            duration_seconds=duration_seconds,
            reason=reason,
            actor=self._actor(actor),
            now=now,
        )

    def end_suppression(
        self,
        occurrence_id: int,
        *,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SuppressionView:
        return self._port.end_suppression(
            occurrence_id,
            expected_version=expected_version,
            actor=self._actor(actor),
            now=now,
        )
