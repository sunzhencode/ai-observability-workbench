"""EventSource configuration: one source, one current configuration.

The physical tables are still called ``eventsourcerevision`` and
``alertmanagerendpointrevision``.  That is a historical name, not a model:
since F22 each source owns exactly one configuration row (``internal_state =
'ACTIVE'``) that is updated in place, plus its endpoint rows and an optional
Thanos history address.  Renaming the tables would drag three foreign keys and
a set of frozen migration checksums along for a nicer name.

Saving is applying.  The guard that a poll cycle cannot see a half-changed
configuration lives in :mod:`app.services.source_polling`, which captures
frozen snapshots at its boundary -- it never depended on immutable rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app import master_key as master_key_module
from app.crypto import LazySecretBox, SecretBox, apply_secret_update, redact_sensitive
from app.registry_models import (
    AlertEndpointObservation,
    AlertmanagerEndpointRevision,
    EndpointPollResult,
    EventSource,
    EventSourceRevision,
    MonitoredCluster,
    SourceGrafanaConfig,
    SourceThanosConfig,
)
from app.models import ConfigAudit, Incident, NotificationDelivery, NotificationRoute
from app.sources.alertmanager import (
    AlertmanagerEndpointClient,
    AlertmanagerEndpointRequest,
)

EVENT_SOURCE_TYPES = {"ALERTMANAGER"}
EVENT_SOURCE_STATES = {"ENABLED", "DISABLED", "ARCHIVED"}
ENDPOINT_AUTH_TYPES = {"NONE", "BEARER", "BASIC"}
# Thanos is read with a bearer token or nothing; the client has never sent
# anything else, so offering BASIC here would only be a control that lies.
THANOS_AUTH_TYPES = {"NONE", "BEARER"}
MAX_ENDPOINTS = 8


def new_event_source_id() -> str:
    """Return an opaque source identity that is independent of mutable URLs."""

    return f"src_{uuid4().hex}"


@dataclass(frozen=True)
class EndpointSnapshot:
    position: int
    canonical_url: str
    enabled: bool
    auth_type: str
    username: str
    secret_configured: bool


@dataclass(frozen=True)
class EndpointTestResult:
    ok: bool
    code: str


@dataclass(frozen=True)
class TestedEndpoint:
    position: int
    ok: bool
    code: str


@dataclass(frozen=True)
class SourceTestResult:
    ok: bool
    code: str
    endpoints: tuple[TestedEndpoint, ...]


class EventSourceTestFailed(ValueError):
    def __init__(self, result: SourceTestResult) -> None:
        super().__init__(result.code)
        self.result = result


class EndpointTester(Protocol):
    async def test(
        self, endpoint: EndpointSnapshot, secret: str
    ) -> EndpointTestResult: ...


class HTTPAlertmanagerEndpointTester:
    """Perform one bounded Alertmanager GET without following redirects."""

    def __init__(self, transport=None) -> None:
        self.client = AlertmanagerEndpointClient(transport=transport)

    async def test(
        self, endpoint: EndpointSnapshot, secret: str
    ) -> EndpointTestResult:
        result = await self.client.fetch_alerts(
            AlertmanagerEndpointRequest(
                base_url=endpoint.canonical_url,
                auth_kind=endpoint.auth_type,
                username=endpoint.username,
                secret=secret,
                timeout_seconds=15,
            )
        )
        code = "OK" if result.ok else result.safe_error_code or "ENDPOINT_NETWORK"
        return EndpointTestResult(result.ok, code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _box(box: SecretBox | None) -> SecretBox | LazySecretBox:
    # Lazy: a resource with no credentials must be savable even if the local
    # key file cannot be created.
    return box or LazySecretBox(lambda: master_key_module.master_key())


def _canonical_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or len(url) > 2048
    ):
        raise ValueError(
            "endpoint URL must be absolute HTTP(S) without credentials/query"
        )
    return url


def _resolved_positions(raw_endpoints: list[dict[str, Any]]) -> list[int]:
    """The slot each submitted endpoint claims.

    A position is a stable slot: it owns the stored credential that ``KEEP``
    resolves against, and `EndpointPollResult` / `AlertEndpointObservation` hold
    foreign keys into the row living there. Deriving it from array order broke
    that the moment a caller removed an endpoint -- everything after it shifted
    down a slot and silently inherited the previous occupant's secret.

    Callers that know their slots send them. Callers that do not (a fresh
    source, or an API client posting a plain list) still get array order, which
    is right for a list nobody has reordered.
    """
    claimed = [raw.get("position") for raw in raw_endpoints]
    if all(item is None for item in claimed):
        return list(range(len(raw_endpoints)))
    if any(item is None for item in claimed):
        raise ValueError("endpoint position must be set on every endpoint or none")
    resolved = [int(item) for item in claimed]
    if any(not 0 <= item < MAX_ENDPOINTS for item in resolved):
        raise ValueError(f"endpoint position must be between 0 and {MAX_ENDPOINTS - 1}")
    if len(set(resolved)) != len(resolved):
        raise ValueError("endpoint positions must be unique within a source")
    return resolved


def _validated_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    name = str(candidate.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("name must contain 1-120 characters")
    raw_endpoints = list(candidate.get("endpoints") or [])
    if not 1 <= len(raw_endpoints) <= MAX_ENDPOINTS:
        raise ValueError(f"endpoints must contain 1-{MAX_ENDPOINTS} items")
    endpoints: list[dict[str, Any]] = []
    urls: set[str] = set()
    positions = _resolved_positions(raw_endpoints)
    for position, raw in zip(positions, raw_endpoints):
        url = _canonical_url(raw.get("url", ""))
        if url.casefold() in urls:
            raise ValueError("endpoint URLs must be unique within a source")
        urls.add(url.casefold())
        auth_type = str(raw.get("auth_type") or "NONE").upper()
        if auth_type not in ENDPOINT_AUTH_TYPES:
            raise ValueError("unsupported endpoint auth_type")
        username = str(raw.get("username") or "").strip()
        if auth_type == "BASIC" and not username:
            raise ValueError("BASIC auth requires username")
        if len(username) > 256:
            raise ValueError("username is too long")
        endpoints.append(
            {
                "position": position,
                "url": url,
                "enabled": bool(raw.get("enabled", True)),
                "auth_type": auth_type,
                "username": username,
                "secret_action": str(raw.get("secret_action") or "KEEP").upper(),
                "secret_value": raw.get("secret_value"),
            }
        )
    if not any(item["enabled"] for item in endpoints):
        raise ValueError("at least one endpoint must be enabled")

    def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
        raw_value = candidate.get(name, default)
        if raw_value is None:
            raw_value = default
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} is outside its allowed range")
        return value

    poll_interval = bounded_int("poll_interval_seconds", 30, 5, 3600)
    resolution_grace = bounded_int("resolution_grace_seconds", 60, 0, 86400)
    max_parallel = bounded_int("max_parallel_endpoints", 4, 1, MAX_ENDPOINTS)
    watchdog_missing = bounded_int(
        "watchdog_missing_after_seconds", max(3 * poll_interval, 60), 60, 86400
    )
    if watchdog_missing < poll_interval:
        raise ValueError("watchdog_missing_after_seconds must be at least poll interval")
    watchdog_alertname = str(
        candidate.get("watchdog_alertname") or "Watchdog"
    ).strip()
    watchdog_identity = str(
        candidate.get("watchdog_identity_label") or "cluster"
    ).strip()
    if not watchdog_alertname or len(watchdog_alertname) > 128:
        raise ValueError("watchdog_alertname must contain 1-128 characters")
    if not watchdog_identity or len(watchdog_identity) > 128:
        raise ValueError("watchdog_identity_label must contain 1-128 characters")
    return {
        "name": name,
        "endpoints": endpoints,
        "poll_interval_seconds": poll_interval,
        "resolution_grace_seconds": resolution_grace,
        "max_parallel_endpoints": max_parallel,
        "watchdog_enabled": bool(candidate.get("watchdog_enabled", False)),
        "watchdog_alertname": watchdog_alertname,
        "watchdog_identity_label": watchdog_identity,
        "watchdog_missing_after_seconds": watchdog_missing,
        "thanos": _validated_thanos(candidate.get("thanos")),
    }


def _validated_thanos(raw: Any) -> dict[str, Any] | None:
    """Validate the optional history address. ``None`` means "do not backfill"."""

    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or "").strip()
    if not url:
        return None
    auth_type = str(raw.get("auth_type") or "NONE").upper()
    if auth_type not in THANOS_AUTH_TYPES:
        raise ValueError("unsupported history auth_type")
    try:
        timeout = int(raw.get("timeout_seconds") or 15)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be an integer") from exc
    if not 1 <= timeout <= 120:
        raise ValueError("timeout_seconds is outside its allowed range")
    username = str(raw.get("username") or "").strip()
    if len(username) > 256:
        raise ValueError("username is too long")
    return {
        "url": _canonical_url(url),
        "auth_type": auth_type,
        "username": username,
        "timeout_seconds": timeout,
        "secret_action": str(raw.get("secret_action") or "KEEP").upper(),
        "secret_value": raw.get("secret_value"),
    }


def _source(session: Session, source_id: str) -> EventSource:
    source = session.get(EventSource, source_id)
    if source is None:
        raise LookupError("event source not found")
    return source


def _check_version(source: EventSource, expected_version: int) -> None:
    if source.version != expected_version:
        raise FileExistsError("event source revision conflict")


def _claim_version(
    session: Session, source: EventSource, expected_version: int
) -> None:
    """Atomically claim one logical-resource version inside the caller UoW."""

    _check_version(source, expected_version)
    result = session.execute(
        update(EventSource)
        .where(
            EventSource.id == source.id,
            EventSource.version == expected_version,
        )
        .values(version=expected_version + 1, updated_at=_now())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise FileExistsError("event source revision conflict")
    session.expire(source)
    session.refresh(source)


def _restart_watchdog_monitoring(session: Session, source_id: str) -> None:
    restarted_at = _now()
    for cluster in session.exec(
        select(MonitoredCluster).where(
            MonitoredCluster.source_id == source_id,
            MonitoredCluster.inventory_state != "IGNORED",
        )
    ).all():
        cluster.monitoring_started_at = restarted_at
        cluster.version += 1
        session.add(cluster)


def _mark_source_stale(session: Session, source_id: str, *, reason: str) -> None:
    incidents = session.exec(
        select(Incident).where(
            Incident.source_id == source_id,
            Incident.superseded_by_incident_id.is_(None),
        )
    ).all()
    for incident in incidents:
        if incident.source_state == "recovered":
            continue
        incident.freshness_state = "STALE"
        session.add(incident)
        routes = session.exec(
            select(NotificationRoute).where(
                NotificationRoute.incident_id == incident.id,
                NotificationRoute.status == "ACTIVE",
            )
        ).all()
        for route in routes:
            route.next_reminder_at = None
            session.add(route)
            deliveries = session.exec(
                select(NotificationDelivery).where(
                    NotificationDelivery.route_id == route.id,
                    NotificationDelivery.state.in_(["PENDING", "RETRY_WAIT"]),
                )
            ).all()
            for delivery in deliveries:
                delivery.state = "CANCELED"
                delivery.suppression_reason = reason
                session.add(delivery)


def _check_name(
    session: Session, name: str, *, source_id: str | None = None
) -> None:
    normalized = name.casefold()
    for item in session.exec(
        select(EventSource).where(EventSource.type == "ALERTMANAGER")
    ).all():
        if item.id != source_id and item.name.casefold() == normalized:
            raise ValueError("event source name must be unique")


def _config_row(session: Session, source_id: str) -> EventSourceRevision | None:
    """The one current configuration row of a source, or None before creation."""

    return session.exec(
        select(EventSourceRevision).where(
            EventSourceRevision.source_id == source_id,
            EventSourceRevision.internal_state == "ACTIVE",
        )
    ).first()


def _endpoints(
    session: Session, revision_id: int | None
) -> list[AlertmanagerEndpointRevision]:
    if revision_id is None:
        return []
    return list(
        session.exec(
            select(AlertmanagerEndpointRevision)
            .where(AlertmanagerEndpointRevision.source_revision_id == revision_id)
            .order_by(AlertmanagerEndpointRevision.position)
        ).all()
    )


def _prepare_endpoints(
    values: dict[str, Any],
    *,
    existing: list[AlertmanagerEndpointRevision],
    box: SecretBox,
) -> list[AlertmanagerEndpointRevision]:
    by_position = {item.position: item for item in existing}
    prepared: list[AlertmanagerEndpointRevision] = []
    for item in values["endpoints"]:
        previous = by_position.get(item["position"])
        envelope = apply_secret_update(
            previous.secret_envelope_json if previous is not None else None,
            item["secret_action"],
            item["secret_value"],
            box,
            required=item["auth_type"] in {"BEARER", "BASIC"},
        )
        if item["auth_type"] == "NONE" and envelope is not None:
            raise ValueError("NONE auth cannot retain or store a secret")
        prepared.append(
            AlertmanagerEndpointRevision(
                source_revision_id=0,
                position=item["position"],
                canonical_url=item["url"],
                enabled=item["enabled"],
                auth_kind=item["auth_type"],
                username=item["username"],
                secret_envelope_json=envelope,
            )
        )
    return prepared


def _snapshot(endpoint: AlertmanagerEndpointRevision) -> EndpointSnapshot:
    return EndpointSnapshot(
        position=endpoint.position,
        canonical_url=endpoint.canonical_url,
        enabled=endpoint.enabled,
        auth_type=endpoint.auth_kind,
        username=endpoint.username,
        secret_configured=endpoint.secret_envelope_json is not None,
    )


async def _test_endpoints(
    endpoints: list[AlertmanagerEndpointRevision],
    *,
    box: SecretBox,
    tester: EndpointTester,
    positions: set[int] | None = None,
) -> SourceTestResult:
    selected = [
        item
        for item in endpoints
        if item.enabled and (positions is None or item.position in positions)
    ]
    if not selected:
        return SourceTestResult(False, "NO_ENABLED_ENDPOINTS", ())
    secrets = [
        ""
        if item.auth_kind == "NONE"
        else box.decrypt(item.secret_envelope_json)
        for item in selected
    ]
    raw_results = await asyncio.gather(
        *(
            tester.test(_snapshot(endpoint), secret)
            for endpoint, secret in zip(selected, secrets, strict=True)
        )
    )
    tested = tuple(
        TestedEndpoint(endpoint.position, result.ok, result.code)
        for endpoint, result in zip(selected, raw_results, strict=True)
    )
    ok = all(item.ok for item in tested)
    return SourceTestResult(ok, "OK" if ok else "ENDPOINT_TEST_FAILED", tested)


def _audit(
    session: Session,
    source_id: str,
    action: str,
    result: str,
    changes: dict[str, Any] | None = None,
) -> None:
    session.add(
        ConfigAudit(
            resource_type="EVENT_SOURCE",
            resource_id=source_id,
            action=action,
            result=result,
            redacted_diff_json=redact_sensitive(changes or {}),
        )
    )


def _write_config_row(
    session: Session,
    source: EventSource,
    values: dict[str, Any],
    prepared: list[AlertmanagerEndpointRevision],
) -> EventSourceRevision:
    """Create or update the source's single configuration row, in place."""

    revision = _config_row(session, source.id)
    if revision is None:
        latest = session.exec(
            select(EventSourceRevision.revision_no)
            .where(EventSourceRevision.source_id == source.id)
            .order_by(EventSourceRevision.revision_no.desc())
        ).first()
        revision = EventSourceRevision(
            source_id=source.id,
            # Retired rows from before F22 still hold their numbers and the
            # (source_id, revision_no) uniqueness, so keep counting past them.
            revision_no=int(latest or 0) + 1,
            internal_state="ACTIVE",
            activated_at=_now(),
        )
    revision.poll_interval_seconds = values["poll_interval_seconds"]
    revision.resolution_grace_seconds = values["resolution_grace_seconds"]
    revision.max_parallel_endpoints = values["max_parallel_endpoints"]
    revision.watchdog_enabled = values["watchdog_enabled"]
    revision.watchdog_alertname = values["watchdog_alertname"]
    revision.watchdog_identity_label = values["watchdog_identity_label"]
    revision.watchdog_missing_after_seconds = values["watchdog_missing_after_seconds"]
    session.add(revision)
    session.flush()
    _sync_endpoints(session, int(revision.id), prepared)
    return revision


def _sync_endpoints(
    session: Session,
    revision_id: int,
    prepared: list[AlertmanagerEndpointRevision],
) -> None:
    """Match endpoint rows by position and update them where they already exist.

    Rows are reused rather than replaced because poll results and alert
    observations carry foreign keys into them: a position is a stable slot, and
    editing its address edits that slot.  A position that disappears takes its
    own runtime history with it -- that history describes an endpoint the user
    just removed, and leaving it would leave a dangling reference.
    """
    existing = {item.position: item for item in _endpoints(session, revision_id)}
    for item in prepared:
        row = existing.pop(item.position, None)
        if row is None:
            item.source_revision_id = revision_id
            session.add(item)
            continue
        row.canonical_url = item.canonical_url
        row.enabled = item.enabled
        row.auth_kind = item.auth_kind
        row.username = item.username
        row.secret_envelope_json = item.secret_envelope_json
        session.add(row)
    for removed in existing.values():
        session.execute(
            delete(AlertEndpointObservation).where(
                AlertEndpointObservation.endpoint_revision_id == removed.id
            )
        )
        session.execute(
            delete(EndpointPollResult).where(
                EndpointPollResult.endpoint_revision_id == removed.id
            )
        )
        session.delete(removed)
    session.flush()


def _thanos_row(session: Session, source_id: str) -> SourceThanosConfig | None:
    return session.exec(
        select(SourceThanosConfig).where(SourceThanosConfig.source_id == source_id)
    ).first()


def _write_thanos(
    session: Session,
    source_id: str,
    values: dict[str, Any] | None,
    *,
    box: SecretBox,
) -> None:
    """Persist the optional history address. ``None`` clears it."""

    row = _thanos_row(session, source_id)
    if values is None:
        if row is not None:
            session.delete(row)
            session.flush()
        return
    envelope = apply_secret_update(
        row.secret_envelope_json if row is not None else None,
        values["secret_action"],
        values["secret_value"],
        box,
        required=values["auth_type"] == "BEARER",
    )
    if values["auth_type"] == "NONE" and envelope is not None:
        raise ValueError("NONE auth cannot retain or store a secret")
    if row is None:
        row = SourceThanosConfig(source_id=source_id)
    changed = row.canonical_url != values["url"]
    row.canonical_url = values["url"]
    row.auth_kind = values["auth_type"]
    row.username = values["username"]
    row.secret_envelope_json = envelope
    row.timeout_seconds = values["timeout_seconds"]
    row.updated_at = _now()
    if changed:
        row.last_test_status = None
        row.tested_at = None
        row.safe_error_code = None
    session.add(row)
    session.flush()


def _thanos_public(session: Session, source_id: str) -> dict[str, Any] | None:
    row = _thanos_row(session, source_id)
    if row is None or not row.canonical_url:
        return None
    return {
        "url": row.canonical_url,
        "auth_type": row.auth_kind,
        "username": row.username,
        "secret_configured": row.secret_envelope_json is not None,
        "timeout_seconds": row.timeout_seconds,
        "last_test_status": row.last_test_status,
        "tested_at": row.tested_at,
        "safe_error_code": row.safe_error_code,
    }


def _grafana_row(session: Session, source_id: str) -> SourceGrafanaConfig | None:
    return session.exec(
        select(SourceGrafanaConfig).where(SourceGrafanaConfig.source_id == source_id)
    ).first()


def write_grafana_config(
    session: Session,
    source_id: str,
    values: dict[str, Any] | None,
    *,
    box: SecretBox,
) -> SourceGrafanaConfig | None:
    """Persist the optional Grafana address. ``None`` — or an empty URL — clears it.

    Written through its own endpoint rather than as part of the source form, and
    that is deliberate: the source form saves and takes effect immediately,
    while importing is gated on an explicit successful test. Folding the two
    together would leave the gate with nothing to hold.

    Changing any connection material clears the test result, because a
    successful test against the previous address, credential or timeout says
    nothing about the new configuration.
    """

    row = _grafana_row(session, source_id)
    url = str((values or {}).get("url") or "").strip()
    if values is None or not url:
        if row is not None:
            session.delete(row)
            session.flush()
        return None

    previous_url = row.base_url if row is not None else ""
    previous_envelope = row.secret_envelope_json if row is not None else None
    previous_timeout = row.timeout_seconds if row is not None else None
    envelope = apply_secret_update(
        previous_envelope,
        values["secret_action"],
        values["secret_value"],
        box,
        required=False,
    )
    if row is None:
        row = SourceGrafanaConfig(source_id=source_id)
    timeout_seconds = int(values.get("timeout_seconds") or 15)
    changed = (
        previous_url != url
        or previous_envelope != envelope
        or previous_timeout != timeout_seconds
    )
    row.base_url = url
    row.secret_envelope_json = envelope
    row.timeout_seconds = timeout_seconds
    row.updated_at = _now()
    if changed:
        row.last_test_status = None
        row.tested_at = None
        row.safe_error_code = None
    session.add(row)
    session.flush()
    return row


def grafana_config_row(
    session: Session, source_id: str
) -> SourceGrafanaConfig | None:
    """Read side, for the client factory and the import gate."""

    return _grafana_row(session, source_id)


def _grafana_public(session: Session, source_id: str) -> dict[str, Any] | None:
    row = _grafana_row(session, source_id)
    if row is None or not row.base_url:
        return None
    return {
        "url": row.base_url,
        # Whether a credential exists, never the credential. An anonymous
        # Grafana is a supported configuration, so `False` here is a normal
        # state and not an unfinished one.
        "secret_configured": row.secret_envelope_json is not None,
        "timeout_seconds": row.timeout_seconds,
        "last_test_status": row.last_test_status,
        "tested_at": row.tested_at,
        "safe_error_code": row.safe_error_code,
    }


def create_event_source(
    session: Session,
    *,
    candidate: dict[str, Any],
    enable: bool = True,
    box: SecretBox | None = None,
) -> EventSource:
    """Register a source from a name and an address, ready to poll.

    A connection test is no longer a precondition.  Requiring one turned the
    minimal path into four verbs and made the workbench unusable while the
    address was briefly unreachable; a wrong address now shows up as a source
    level poll failure instead, which is where the user looks anyway.
    """
    values = _validated_candidate(candidate)
    _check_name(session, values["name"])
    secret_box = _box(box)
    prepared = _prepare_endpoints(values, existing=[], box=secret_box)

    source = EventSource(
        id=new_event_source_id(),
        type="ALERTMANAGER",
        name=values["name"],
        lifecycle_state="ENABLED" if enable else "DISABLED",
    )
    session.add(source)
    session.flush()
    revision = _write_config_row(session, source, values, prepared)
    _write_thanos(session, source.id, values["thanos"], box=secret_box)
    source.active_revision_id = revision.id
    session.add(source)
    _audit(session, source.id, "CREATE", "SUCCESS", {"enable": enable})
    session.flush()
    return source


def save_event_source_changes(
    session: Session,
    source_id: str,
    *,
    candidate: dict[str, Any],
    expected_version: int,
    box: SecretBox | None = None,
) -> EventSource:
    """Update the source's configuration in place. Saving is applying."""

    source = _source(session, source_id)
    _check_version(source, expected_version)
    if source.lifecycle_state == "ARCHIVED":
        raise ValueError("archived event source is read-only")
    values = _validated_candidate(candidate)
    _check_name(session, values["name"], source_id=source.id)
    secret_box = _box(box)
    current = _config_row(session, source.id)
    existing = _endpoints(session, current.id if current is not None else None)
    prepared = _prepare_endpoints(values, existing=existing, box=secret_box)
    _claim_version(session, source, expected_version)
    revision = _write_config_row(session, source, values, prepared)
    _write_thanos(session, source.id, values["thanos"], box=secret_box)
    source.active_revision_id = revision.id
    source.name = values["name"]
    session.add(source)
    _audit(session, source.id, "SAVE", "SUCCESS", {"endpoints": len(prepared)})
    session.flush()
    return source


def _record_test_audits(
    session: Session,
    source_id: str,
    config_version: int,
    result: SourceTestResult,
) -> None:
    for endpoint in result.endpoints:
        _audit(
            session,
            source_id,
            "TEST_ENDPOINT",
            "SUCCESS" if endpoint.ok else "FAILED",
            {
                "config_version": config_version,
                "position": endpoint.position,
                "code": endpoint.code,
                "ok": endpoint.ok,
            },
        )


async def test_event_source(
    session: Session,
    source_id: str,
    *,
    candidate: dict[str, Any] | None = None,
    positions: set[int] | None = None,
    box: SecretBox | None = None,
    tester: EndpointTester | None = None,
) -> SourceTestResult:
    source = _source(session, source_id)
    if source.lifecycle_state == "ARCHIVED":
        raise ValueError("archived event source is read-only")
    persisted_revision: EventSourceRevision | None = None
    if candidate is None:
        persisted_revision = _config_row(session, source.id)
        if persisted_revision is None:
            raise LookupError("event source configuration not found")
        prepared = _endpoints(session, persisted_revision.id)
        config_version = persisted_revision.revision_no
    else:
        values = _validated_candidate(candidate)
        base = _config_row(session, source.id)
        prepared = _prepare_endpoints(
            values,
            existing=_endpoints(session, base.id if base is not None else None),
            box=_box(box),
        )
        config_version = 0

    result = await _test_endpoints(
        prepared,
        box=_box(box),
        tester=tester or HTTPAlertmanagerEndpointTester(),
        positions=positions,
    )
    if persisted_revision is not None:
        enabled_positions = {item.position for item in prepared if item.enabled}
        tested_positions = {item.position for item in result.endpoints}
        full_test = positions is None or tested_positions == enabled_positions
        persisted_revision.last_test_status = (
            "SUCCESS"
            if result.ok and full_test
            else "FAILED"
            if not result.ok
            else "PARTIAL"
        )
        persisted_revision.tested_at = _now()
        persisted_revision.safe_error_code = None if result.ok else result.code
        session.add(persisted_revision)
    _record_test_audits(session, source.id, config_version, result)
    _audit(
        session,
        source.id,
        "TEST",
        "SUCCESS" if result.ok else "FAILED",
        {"code": result.code, "config_version": config_version},
    )
    session.flush()
    return result


test_event_source.__test__ = False


def enable_event_source(
    session: Session, source_id: str, *, expected_version: int
) -> EventSource:
    source = _source(session, source_id)
    _check_version(source, expected_version)
    if source.lifecycle_state == "ARCHIVED":
        raise ValueError("archived event source cannot be enabled")
    was_disabled = source.lifecycle_state == "DISABLED"
    current = _config_row(session, source.id)
    if current is None:
        raise EventSourceTestFailed(
            SourceTestResult(False, "NO_CONFIGURATION", ())
        )
    _claim_version(session, source, expected_version)
    source.active_revision_id = current.id
    source.lifecycle_state = "ENABLED"
    source.archived_at = None
    session.add(source)
    if was_disabled:
        _restart_watchdog_monitoring(session, source.id)
    _audit(session, source.id, "ENABLE", "SUCCESS")
    session.flush()
    return source


def disable_event_source(
    session: Session, source_id: str, *, expected_version: int
) -> EventSource:
    source = _source(session, source_id)
    _check_version(source, expected_version)
    if source.lifecycle_state == "ARCHIVED":
        raise ValueError("archived event source cannot be disabled")
    _claim_version(session, source, expected_version)
    source.lifecycle_state = "DISABLED"
    session.add(source)
    _mark_source_stale(session, source.id, reason="SOURCE_DISABLED")
    _audit(session, source.id, "DISABLE", "SUCCESS")
    session.flush()
    return source


def archive_event_source(
    session: Session, source_id: str, *, expected_version: int
) -> EventSource:
    source = _source(session, source_id)
    _check_version(source, expected_version)
    if source.lifecycle_state == "ENABLED":
        raise ValueError("event source must be disabled before archive")
    _claim_version(session, source, expected_version)
    source.lifecycle_state = "ARCHIVED"
    source.archived_at = _now()
    session.add(source)
    _mark_source_stale(session, source.id, reason="SOURCE_ARCHIVED")
    _audit(session, source.id, "ARCHIVE", "SUCCESS")
    session.flush()
    return source


def _endpoint_test_audits(
    session: Session, source_id: str, config_version: int
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    audits = session.exec(
        select(ConfigAudit)
        .where(
            ConfigAudit.resource_type == "EVENT_SOURCE",
            ConfigAudit.resource_id == source_id,
            ConfigAudit.action == "TEST_ENDPOINT",
        )
        .order_by(ConfigAudit.id.desc())
    ).all()
    for audit in audits:
        values = dict(audit.redacted_diff_json or {})
        if values.get("config_version") != config_version:
            continue
        position = int(values.get("position", -1))
        if position >= 0 and position not in result:
            result[position] = {
                "ok": bool(values.get("ok")),
                "code": str(values.get("code") or "UNKNOWN"),
                "tested_at": audit.created_at,
            }
    return result


def _config_public(
    session: Session, source_id: str, revision: EventSourceRevision | None
) -> dict[str, Any] | None:
    if revision is None:
        return None
    tests = _endpoint_test_audits(session, source_id, revision.revision_no)
    endpoints = []
    for endpoint in _endpoints(session, revision.id):
        endpoints.append(
            {
                "position": endpoint.position,
                "url": endpoint.canonical_url,
                "enabled": endpoint.enabled,
                "auth_type": endpoint.auth_kind,
                "username": endpoint.username,
                "secret_configured": endpoint.secret_envelope_json is not None,
                "last_test": tests.get(endpoint.position),
            }
        )
    return {
        "poll_interval_seconds": revision.poll_interval_seconds,
        "resolution_grace_seconds": revision.resolution_grace_seconds,
        "max_parallel_endpoints": revision.max_parallel_endpoints,
        "watchdog_enabled": revision.watchdog_enabled,
        "watchdog_alertname": revision.watchdog_alertname,
        "watchdog_identity_label": revision.watchdog_identity_label,
        "watchdog_missing_after_seconds": revision.watchdog_missing_after_seconds,
        "last_test_status": revision.last_test_status,
        "tested_at": revision.tested_at,
        "safe_error_code": revision.safe_error_code,
        "endpoints": endpoints,
        "thanos": _thanos_public(session, source_id),
        "grafana": _grafana_public(session, source_id),
    }


def event_source_public_dict(
    session: Session, source: EventSource
) -> dict[str, Any]:
    current = _config_row(session, source.id)
    if source.lifecycle_state == "ARCHIVED":
        status = "ARCHIVED"
    elif source.lifecycle_state == "DISABLED":
        status = "DISABLED"
    elif current is not None and current.last_test_status == "FAILED":
        status = "CONNECTION_ERROR"
    else:
        status = "ENABLED"
    return {
        "id": source.id,
        "type": source.type,
        "name": source.name,
        "lifecycle_state": source.lifecycle_state,
        "status": status,
        "version": source.version,
        "config": _config_public(session, source.id, current),
        "created_at": source.created_at,
        "updated_at": source.updated_at,
        "archived_at": source.archived_at,
    }


def has_managed_event_source(session: Session) -> bool:
    """Whether a non-archived EventSource exists.

    Migration 5 creates ARCHIVED placeholders for pre-F20 `.env`/ConnectionProfile
    config.  Those placeholders must never count as "the registry is populated":
    doing so suppressed the legacy import prompt on a workbench whose only real
    source was still the `.env` fallback. See
    docs/superpowers/specs/archive/HISTORY.md §3.3.
    """
    return (
        session.exec(
            select(EventSource.id).where(EventSource.lifecycle_state != "ARCHIVED")
        ).first()
        is not None
    )


def list_event_sources(
    session: Session, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    statement = select(EventSource)
    if not include_archived:
        statement = statement.where(EventSource.lifecycle_state != "ARCHIVED")
    return [
        event_source_public_dict(session, item)
        for item in session.exec(
            statement.order_by(EventSource.name, EventSource.id)
        ).all()
    ]


def event_source_audit_public(
    session: Session, source_id: str
) -> list[dict[str, Any]]:
    _source(session, source_id)
    return [
        {
            "id": item.id,
            "action": item.action,
            "actor": item.actor,
            "result": item.result,
            "changes": dict(item.redacted_diff_json or {}),
            "created_at": item.created_at,
        }
        for item in session.exec(
            select(ConfigAudit)
            .where(
                ConfigAudit.resource_type == "EVENT_SOURCE",
                ConfigAudit.resource_id == source_id,
            )
            .order_by(ConfigAudit.id.desc())
        ).all()
    ]
