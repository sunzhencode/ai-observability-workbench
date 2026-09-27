"""Deterministic initial state for an operational Incident occurrence."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from app.domains.incidents.response import ResponseState

__all__ = ["ResponseState"]


class SignalState(StrEnum):
    FIRING = "FIRING"
    RECOVERED = "RECOVERED"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


class AckSlaState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    ON_TRACK = "ON_TRACK"
    AT_RISK = "AT_RISK"
    BREACHED = "BREACHED"
    STOPPED = "STOPPED"


UNMAPPED_ACK_SLA_SECONDS = 15 * 60
ACK_SLA_AT_RISK_PERCENT = 20


def signal_state(source_state: str, freshness_state: str) -> SignalState:
    if freshness_state == "STALE":
        return SignalState.STALE
    try:
        return SignalState(source_state)
    except ValueError:
        return SignalState.UNKNOWN


def ack_sla_at_risk_at(
    *, due_at: datetime, ack_sla_seconds: int
) -> datetime:
    """Return the inclusive start of the frozen SLA's final 20 percent."""

    return due_at - timedelta(
        seconds=ack_sla_seconds * ACK_SLA_AT_RISK_PERCENT / 100
    )


def ack_sla_state(
    *,
    due_at: datetime | None,
    ack_sla_seconds: int,
    acknowledged_at: datetime | None,
    now: datetime,
) -> AckSlaState:
    if acknowledged_at is not None:
        return AckSlaState.STOPPED
    if due_at is None:
        return AckSlaState.NOT_STARTED
    if now >= due_at:
        return AckSlaState.BREACHED
    if now >= ack_sla_at_risk_at(
        due_at=due_at, ack_sla_seconds=ack_sla_seconds
    ):
        return AckSlaState.AT_RISK
    return AckSlaState.ON_TRACK
