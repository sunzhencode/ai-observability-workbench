"""Deterministic Event Source collection facts without persistence or HTTP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Mapping, cast


class SourceState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    ARCHIVED = "ARCHIVED"


class PollCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    enabled: bool = False
    alertname: str = "Watchdog"
    identity_label: str = "cluster"
    missing_after_seconds: int = 90


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    source_id: str
    source_version: int
    position: int
    canonical_url: str
    auth_kind: str
    username: str
    secret: str = field(default="", repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_id: str
    source_name: str
    version: int
    state: SourceState
    poll_interval_seconds: int
    resolution_grace_seconds: int
    max_parallel_endpoints: int
    endpoints: tuple[EndpointSnapshot, ...]
    watchdog: WatchdogConfig = WatchdogConfig()


RawAlert = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    endpoint: EndpointSnapshot
    status: str
    alerts: tuple[RawAlert, ...]
    duration_ms: int
    safe_error_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCESS" and self.safe_error_code is None


@dataclass(frozen=True, slots=True)
class MergedAlert:
    identity: str
    raw: RawAlert
    endpoint_positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    completeness: PollCompleteness
    alerts: tuple[MergedAlert, ...]
    endpoint_observations: tuple[EndpointObservation, ...]
    safe_error_codes: tuple[str, ...]


def _labels(raw: RawAlert) -> Mapping[str, object]:
    value = raw.get("labels")
    return value if isinstance(value, Mapping) else {}


def alert_identity(raw: RawAlert) -> str:
    fingerprint = str(raw.get("fingerprint") or "").strip()
    if fingerprint:
        return fingerprint
    canonical = json.dumps(
        {str(key): str(value) for key, value in sorted(_labels(raw).items())},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"labels:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _timestamp(raw: RawAlert) -> float:
    for key in ("updatedAt", "startsAt"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    return 0.0


def _payload(raw: RawAlert) -> str:
    return json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def merge_endpoint_observations(
    observations: tuple[EndpointObservation, ...],
) -> CollectionOutcome:
    """Union one HA domain and deterministically choose divergent payloads."""
    succeeded = tuple(item for item in observations if item.succeeded)
    if succeeded and len(succeeded) == len(observations):
        completeness = PollCompleteness.COMPLETE
    elif succeeded:
        completeness = PollCompleteness.PARTIAL
    else:
        completeness = PollCompleteness.FAILED

    codes = {
        item.safe_error_code
        for item in observations
        if item.safe_error_code is not None
    }
    records: dict[str, dict[str, object]] = {}
    for observation in succeeded:
        for raw in observation.alerts:
            identity = alert_identity(raw)
            payload = _payload(raw)
            choice = (_timestamp(raw), -observation.endpoint.position)
            current = records.get(identity)
            if current is None:
                records[identity] = {
                    "raw": raw,
                    "payload": payload,
                    "choice": choice,
                    "positions": {observation.endpoint.position},
                }
                continue
            positions = current["positions"]
            if not isinstance(positions, set):
                raise RuntimeError("invalid merge accumulator")
            positions.add(observation.endpoint.position)
            if current["payload"] != payload:
                codes.add("PAYLOAD_DIVERGENCE")
                if choice > current["choice"]:  # type: ignore[operator]
                    current["raw"] = raw
                    current["payload"] = payload
                    current["choice"] = choice

    if completeness is PollCompleteness.PARTIAL:
        codes.add("PARTIAL_POLL")
    elif completeness is PollCompleteness.FAILED:
        codes.add("ALL_ENDPOINTS_FAILED")

    merged: list[MergedAlert] = []
    for identity, record in sorted(records.items()):
        positions = record["positions"]
        raw_value = record["raw"]
        if not isinstance(positions, set) or not isinstance(raw_value, Mapping):
            raise RuntimeError("invalid merge accumulator")
        raw = cast(RawAlert, raw_value)
        merged.append(
            MergedAlert(
                identity=identity,
                raw=raw,
                endpoint_positions=tuple(sorted(int(value) for value in positions)),
            )
        )
    return CollectionOutcome(
        completeness=completeness,
        alerts=tuple(merged),
        endpoint_observations=observations,
        safe_error_codes=tuple(sorted(codes)),
    )
