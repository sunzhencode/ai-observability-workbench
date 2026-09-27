"""Small versioned SQLite migration runner for the local single-process app."""

from __future__ import annotations

import hashlib
import inspect as python_inspect
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import Connection, Engine, inspect, text
from sqlmodel import SQLModel

from app.f20_models import (
    ModelCallLog,
    HistoricalDataSourceRevision,
    AggregationRuleSource,
    AlertEndpointObservation,
    AlertmanagerEndpointRevision,
    DeliveryEvidenceSnapshot,
    EndpointPollResult,
    EventSource,
    EventSourceRevision,
    HistoricalDataSource,
    IncidentOccurrence,
    Investigation,
    MetricQueryTemplate,
    MetricTemplateBaseline,
    MetricTemplateExtras,
    MetricTemplateOrigin,
    MetricTemplateSourceScope,
    ModelChannel,
    ModelChannelRevision,
    MonitoredCluster,
    NotificationPolicySource,
    SourceEvidenceBinding,
    SourceGrafanaConfig,
    SourcePollRun,
    SourceThanosConfig,
)
from app.services.group_keys import build_group_key_v2
from app.services.source_identity import (
    canonical_alertmanager_url,
    source_id_for_alertmanager,
)


class MigrationError(RuntimeError):
    pass


class MigrationChecksumError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    signature: str
    apply: Callable[[Any], dict[str, int] | None]
    transactional: bool = False
    checksum_dependencies: tuple[Callable[..., Any], ...] = ()

    @property
    def checksum(self) -> str:
        raw = (
            f"{self.version}:{self.name}:{self.signature}:"
            f"{python_inspect.getsource(self.apply)}"
        )
        if self.checksum_dependencies:
            raw += ":" + ":".join(
                python_inspect.getsource(item) for item in self.checksum_dependencies
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationReport:
    applied_versions: list[int]
    backup_path: Path | None
    details: dict[int, dict[str, int]] = field(default_factory=dict)


def _column_names(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return set()
    return {str(item["name"]) for item in inspector.get_columns(table_name)}


def _add_column(engine: Engine, table_name: str, name: str, ddl: str) -> None:
    if name in _column_names(engine, table_name):
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


def _migration_1_m1_compatibility(engine: Engine) -> None:
    inspector = inspect(engine)
    if inspector.has_table("alert"):
        _add_column(engine, "alert", "missing_since_at", "DATETIME")
        _add_column(
            engine,
            "alert",
            "source_id",
            "VARCHAR NOT NULL DEFAULT 'legacy'",
        )
        _add_column(engine, "alert", "upstream_fingerprint", "VARCHAR")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE alert SET upstream_fingerprint = fingerprint "
                    "WHERE upstream_fingerprint IS NULL OR upstream_fingerprint = ''"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_alert_source_id ON alert (source_id)"
                )
            )
    if inspector.has_table("incident"):
        _add_column(
            engine,
            "incident",
            "source_id",
            "VARCHAR NOT NULL DEFAULT 'legacy'",
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_incident_source_id "
                    "ON incident (source_id)"
                )
            )


def _occurrence_backfill_expression(engine: Engine) -> str:
    alert_columns = _column_names(engine, "alert")
    incident_columns = _column_names(engine, "incident")
    fallbacks: list[str] = []
    if {"incident_id", "source_state"} <= alert_columns:
        timestamp_terms = [
            name for name in ("starts_at", "first_seen_at") if name in alert_columns
        ]
        if timestamp_terms:
            alert_time = (
                "COALESCE("
                + ", ".join(f"a.{name}" for name in timestamp_terms)
                + ")"
            )
            fallbacks.append(
                "(SELECT MIN("
                + alert_time
                + ") FROM alert a WHERE a.incident_id = incident.id "
                "AND a.source_state != 'resolved')"
            )
    for name in ("updated_at", "created_at"):
        if name in incident_columns:
            fallbacks.append(name)
    fallbacks.append("CURRENT_TIMESTAMP")
    if len(fallbacks) == 1:
        return fallbacks[0]
    return "COALESCE(" + ", ".join(fallbacks) + ")"


def _migration_2_f17_foundation(engine: Engine) -> None:
    if inspect(engine).has_table("incident"):
        additions = (
            ("aggregation_rule_id", "INTEGER"),
            ("aggregation_rule_version", "INTEGER"),
            ("group_labels", "JSON NOT NULL DEFAULT '{}'"),
            ("missing_group_labels", "JSON NOT NULL DEFAULT '[]'"),
            ("occurrence_no", "INTEGER NOT NULL DEFAULT 1"),
            ("occurrence_started_at", "DATETIME"),
            ("change_version", "INTEGER NOT NULL DEFAULT 0"),
            (
                "change_origin",
                "VARCHAR NOT NULL DEFAULT 'SOURCE_ACTIVATION'",
            ),
        )
        for name, ddl in additions:
            _add_column(engine, "incident", name, ddl)
        expression = _occurrence_backfill_expression(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE incident SET occurrence_started_at = "
                    + expression
                    + " WHERE occurrence_started_at IS NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_incident_aggregation_rule_id "
                    "ON incident (aggregation_rule_id)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_incident_occurrence_no "
                    "ON incident (occurrence_no)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_incident_change_origin "
                    "ON incident (change_origin)"
                )
            )

    # Existing tables are left untouched; create_all only adds the new F17 tables.
    with engine.begin() as connection:
        SQLModel.metadata.create_all(connection)


def _migration_3_runtime_settings(engine: Engine) -> None:
    with engine.begin() as connection:
        SQLModel.metadata.create_all(connection)


def _migration_4_delivery_manual_trigger(engine: Engine) -> None:
    if inspect(engine).has_table("notificationdelivery"):
        _add_column(
            engine,
            "notificationdelivery",
            "next_attempt_trigger",
            "VARCHAR NOT NULL DEFAULT 'AUTO'",
        )


def _connection_column_names(connection: Connection, table_name: str) -> set[str]:
    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(item["name"]) for item in inspector.get_columns(table_name)}


def _add_connection_column(
    connection: Connection, table_name: str, name: str, ddl: str
) -> None:
    if name in _connection_column_names(connection, table_name):
        return
    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


def _decoded_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _profile_source_id(row: Any) -> str:
    explicit = str(row.source_id or "").strip()
    if explicit:
        return explicit
    return source_id_for_alertmanager(str(row.base_url or ""))


def _safe_source_name(source_id: str, *, enabled_name: str | None = None) -> str:
    if enabled_name:
        return str(enabled_name)[:120]
    if source_id == "legacy":
        return "Archived legacy source"
    suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
    return f"Archived Alertmanager {suffix}"


def _migration_datetime_key(value: Any) -> float:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _f20_incident_key(
    row: Any,
    *,
    rule_group_labels: dict[int, list[str]],
    incident_fingerprints: dict[int, list[str]],
) -> str:
    source_id = str(row.source_id or "legacy")
    rule_id = int(row.aggregation_rule_id) if row.aggregation_rule_id is not None else None
    group_labels = _decoded_json(row.group_labels, {})
    missing = _decoded_json(row.missing_group_labels, [])
    ordered_names = rule_group_labels.get(rule_id or -1, list(group_labels))
    ordered_values = [
        (name, str(group_labels[name]))
        for name in ordered_names
        if name in group_labels and str(group_labels[name]).strip()
    ]
    incomplete = bool(missing) or any(
        name not in group_labels or not str(group_labels[name]).strip()
        for name in ordered_names
    )
    isolation: str | None = None
    if rule_id is None or incomplete:
        fingerprints = incident_fingerprints.get(int(row.id), [])
        isolation = (
            fingerprints[0]
            if fingerprints
            else "legacy-"
            + hashlib.sha256(str(row.group_key).encode("utf-8")).hexdigest()[:16]
        )
        ordered_values = []
    return build_group_key_v2(
        source_id=source_id,
        rule_id=rule_id,
        ordered_group_values=ordered_values,
        isolation_fingerprint=isolation,
    )


def _migrate_f20_incidents(connection: Connection) -> dict[str, int]:
    if not inspect(connection).has_table("incident"):
        return {
            "collision_count": 0,
            "incident_count": 0,
            "superseded_incident_count": 0,
        }
    required_incident_columns = {
        "id",
        "source_id",
        "group_key",
        "source_state",
        "aggregation_rule_id",
        "group_labels",
        "missing_group_labels",
        "updated_at",
    }
    if not required_incident_columns <= _connection_column_names(
        connection, "incident"
    ):
        return {
            "collision_count": 0,
            "incident_count": 0,
            "superseded_incident_count": 0,
        }

    rule_group_labels: dict[int, list[str]] = {}
    if inspect(connection).has_table("aggregationrule"):
        for row in connection.execute(
            text("SELECT id, group_by_labels FROM aggregationrule")
        ).all():
            rule_group_labels[int(row.id)] = [
                str(item) for item in _decoded_json(row.group_by_labels, [])
            ]

    incident_fingerprints: dict[int, list[str]] = {}
    alert_columns = _connection_column_names(connection, "alert")
    if {"id", "incident_id", "fingerprint"} <= alert_columns:
        upstream_expression = (
            "upstream_fingerprint"
            if "upstream_fingerprint" in alert_columns
            else "fingerprint"
        )
        for row in connection.execute(
            text(
                "SELECT incident_id, "
                + upstream_expression
                + " AS upstream_fingerprint, fingerprint FROM alert "
                "WHERE incident_id IS NOT NULL ORDER BY id"
            )
        ).all():
            fingerprint = str(row.upstream_fingerprint or row.fingerprint or "")
            if fingerprint:
                incident_fingerprints.setdefault(int(row.incident_id), []).append(
                    fingerprint
                )

    incidents = connection.execute(
        text(
            "SELECT id, source_id, group_key, source_state, aggregation_rule_id, "
            "group_labels, missing_group_labels, updated_at "
            "FROM incident ORDER BY id"
        )
    ).all()
    by_v2_key: dict[str, list[Any]] = {}
    for row in incidents:
        by_v2_key.setdefault(
            _f20_incident_key(
                row,
                rule_group_labels=rule_group_labels,
                incident_fingerprints=incident_fingerprints,
            ),
            [],
        ).append(row)

    collision_count = 0
    superseded_count = 0
    for v2_key, rows in sorted(by_v2_key.items()):
        canonical = min(
            rows,
            key=lambda item: (
                0 if str(item.source_state) != "recovered" else 1,
                -_migration_datetime_key(item.updated_at),
                int(item.id),
            ),
        )
        superseded = [item for item in rows if int(item.id) != int(canonical.id)]
        if superseded:
            collision_count += 1
        for row in superseded:
            historical_key = (
                f"superseded={int(row.id)}|"
                + hashlib.sha256(str(row.group_key).encode("utf-8")).hexdigest()
            )
            connection.execute(
                text(
                    "UPDATE incident SET group_key = :historical_key, "
                    "legacy_group_key = COALESCE(legacy_group_key, :legacy_group_key), "
                    "group_key_version = 1, superseded_by_incident_id = :canonical_id, "
                    "change_origin = 'MIGRATION' WHERE id = :incident_id"
                ),
                {
                    "historical_key": historical_key,
                    "legacy_group_key": str(row.group_key),
                    "canonical_id": int(canonical.id),
                    "incident_id": int(row.id),
                },
            )
            if "incident_id" in alert_columns:
                connection.execute(
                    text(
                        "UPDATE alert SET incident_id = :canonical_id "
                        "WHERE incident_id = :superseded_id"
                    ),
                    {
                        "canonical_id": int(canonical.id),
                        "superseded_id": int(row.id),
                    },
                )
            superseded_count += 1
        connection.execute(
            text(
                "UPDATE incident SET group_key = :group_key, "
                "legacy_group_key = COALESCE(legacy_group_key, :legacy_group_key), "
                "group_key_version = 2, superseded_by_incident_id = NULL, "
                "change_origin = 'MIGRATION' WHERE id = :incident_id"
            ),
            {
                "group_key": v2_key,
                "legacy_group_key": str(canonical.group_key),
                "incident_id": int(canonical.id),
            },
        )

    return {
        "collision_count": collision_count,
        "incident_count": len(incidents),
        "superseded_incident_count": superseded_count,
    }


def _migration_5_f20_event_source_foundation(
    connection: Connection,
) -> dict[str, int]:
    from app.f20_models import F20Model

    F20Model.metadata.create_all(connection)
    if inspect(connection).has_table("aggregationrule"):
        _add_connection_column(
            connection,
            "aggregationrule",
            "scope_mode",
            "VARCHAR NOT NULL DEFAULT 'ALL'",
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_aggregationrule_scope_mode "
                "ON aggregationrule (scope_mode)"
            )
        )
    if inspect(connection).has_table("notificationpolicyrevision"):
        _add_connection_column(
            connection,
            "notificationpolicyrevision",
            "scope_mode",
            "VARCHAR NOT NULL DEFAULT 'ALL'",
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_notificationpolicyrevision_scope_mode "
                "ON notificationpolicyrevision (scope_mode)"
            )
        )
    if inspect(connection).has_table("incident"):
        _add_connection_column(
            connection, "incident", "freshness_state", "VARCHAR NOT NULL DEFAULT 'FRESH'"
        )
        _add_connection_column(
            connection,
            "incident",
            "superseded_by_incident_id",
            "INTEGER REFERENCES incident(id)",
        )
        _add_connection_column(
            connection, "incident", "group_key_version", "INTEGER NOT NULL DEFAULT 1"
        )
        _add_connection_column(connection, "incident", "legacy_group_key", "VARCHAR")
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_incident_freshness_state "
                "ON incident (freshness_state)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_incident_superseded_by_incident_id "
                "ON incident (superseded_by_incident_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_incident_group_key_version "
                "ON incident (group_key_version)"
            )
        )

    for table_name in ("alert", "incident"):
        if "source_id" in _connection_column_names(connection, table_name):
            connection.execute(
                text(
                    f"UPDATE {table_name} SET source_id = 'legacy' "
                    "WHERE source_id IS NULL OR trim(source_id) = ''"
                )
            )

    profiles: list[Any] = []
    if inspect(connection).has_table("connectionprofile"):
        profiles = list(
            connection.execute(
                text(
                    "SELECT id, logical_id, version, state, name, base_url, auth_type, "
                    "username, secret_envelope, settings_json, source_id, "
                    "last_tested_at, last_test_result, last_test_error_code, "
                    "created_at, activated_at FROM connectionprofile "
                    "WHERE kind = 'ALERTMANAGER' ORDER BY id"
                )
            ).all()
        )
        profiles = [
            row
            for row in profiles
            if str(row.source_id or "").strip()
            or str(row.state) in {"ACTIVE", "RETIRED"}
            or row.activated_at is not None
        ]

    profile_sources = {_profile_source_id(row): row for row in profiles}
    active_profiles = {
        _profile_source_id(row): row for row in profiles if str(row.state) == "ACTIVE"
    }
    source_ids = set(profile_sources)
    for table_name in ("alert", "incident"):
        if "source_id" not in _connection_column_names(connection, table_name):
            continue
        source_ids.update(
            str(row[0])
            for row in connection.execute(
                text(
                    f"SELECT DISTINCT source_id FROM {table_name} "
                    "WHERE source_id IS NOT NULL AND trim(source_id) != ''"
                )
            ).all()
        )

    now = datetime.now(timezone.utc)
    for source_id in sorted(source_ids):
        active_profile = active_profiles.get(source_id)
        lifecycle = "ENABLED" if active_profile is not None else "ARCHIVED"
        connection.execute(
            text(
                "INSERT OR IGNORE INTO eventsource "
                "(id, type, name, lifecycle_state, active_revision_id, created_at, "
                "updated_at, archived_at, version) "
                "VALUES (:id, 'ALERTMANAGER', :name, :state, NULL, :now, :now, "
                ":archived_at, 1)"
            ),
            {
                "id": source_id,
                "name": _safe_source_name(
                    source_id,
                    enabled_name=(
                        str(active_profile.name) if active_profile is not None else None
                    ),
                ),
                "state": lifecycle,
                "now": now,
                "archived_at": now if lifecycle == "ARCHIVED" else None,
            },
        )

    revision_numbers: dict[str, int] = {}
    for row in profiles:
        source_id = _profile_source_id(row)
        revision_numbers[source_id] = revision_numbers.get(source_id, 0) + 1
        settings = _decoded_json(row.settings_json, {})
        poll_interval = max(5, min(int(settings.get("poll_interval_seconds", 30)), 3600))
        resolution_grace = max(
            0, min(int(settings.get("resolution_grace_seconds", 60)), 86400)
        )
        internal_state = "ACTIVE" if str(row.state) == "ACTIVE" else "RETIRED"
        result = connection.execute(
            text(
                "INSERT INTO eventsourcerevision "
                "(source_id, revision_no, internal_state, poll_interval_seconds, "
                "resolution_grace_seconds, max_parallel_endpoints, watchdog_enabled, "
                "watchdog_alertname, watchdog_identity_label, "
                "watchdog_missing_after_seconds, thanos_binding_id, last_test_status, "
                "tested_at, safe_error_code, legacy_connection_profile_id, created_at, "
                "activated_at) VALUES "
                "(:source_id, :revision_no, :internal_state, :poll_interval, "
                ":resolution_grace, 4, 0, 'Watchdog', 'cluster', :missing_after, NULL, "
                ":test_status, :tested_at, :safe_error_code, :profile_id, :created_at, "
                ":activated_at)"
            ),
            {
                "source_id": source_id,
                "revision_no": revision_numbers[source_id],
                "internal_state": internal_state,
                "poll_interval": poll_interval,
                "resolution_grace": resolution_grace,
                "missing_after": max(3 * poll_interval, 60),
                "test_status": row.last_test_result,
                "tested_at": row.last_tested_at,
                "safe_error_code": row.last_test_error_code,
                "profile_id": int(row.id),
                "created_at": row.created_at or now,
                "activated_at": row.activated_at if internal_state == "ACTIVE" else None,
            },
        )
        revision_id = int(result.lastrowid)
        connection.execute(
            text(
                "INSERT INTO alertmanagerendpointrevision "
                "(source_revision_id, position, canonical_url, enabled, auth_kind, "
                "username, secret_envelope_json) VALUES "
                "(:revision_id, 0, :url, 1, :auth_kind, :username, :secret_envelope)"
            ),
            {
                "revision_id": revision_id,
                "url": canonical_alertmanager_url(str(row.base_url)),
                "auth_kind": str(row.auth_type or "NONE"),
                "username": str(row.username or ""),
                "secret_envelope": (
                    json.dumps(_decoded_json(row.secret_envelope, None))
                    if row.secret_envelope is not None
                    else None
                ),
            },
        )
        if internal_state == "ACTIVE":
            connection.execute(
                text(
                    "UPDATE eventsource SET active_revision_id = :revision_id, "
                    "lifecycle_state = 'ENABLED', archived_at = NULL, updated_at = :now "
                    "WHERE id = :source_id"
                ),
                {"revision_id": revision_id, "source_id": source_id, "now": now},
            )

    incident_stats = _migrate_f20_incidents(connection)
    enabled_count = len(active_profiles)
    return {
        "archived_source_count": len(source_ids) - enabled_count,
        "enabled_source_count": enabled_count,
        "source_count": len(source_ids),
        **incident_stats,
    }



# --- F21 adoption -----------------------------------------------------------
# The registry becomes the only source of truth, so an upgrade must carry the
# working `.env`/ConnectionProfile setup into it -- otherwise removing the legacy
# poll path silently orphans every existing Alert and Incident.
# See docs/adr/0004-registry-only-configuration.md.


def _f21_registry_in_use(connection: Connection) -> bool:
    """A non-archived source means the user already manages the registry."""
    row = connection.execute(
        text("SELECT 1 FROM eventsource WHERE lifecycle_state != 'ARCHIVED' LIMIT 1")
    ).first()
    return row is not None


def _f21_active_profile(connection: Connection, kind: str) -> Any:
    if not inspect(connection).has_table("connectionprofile"):
        return None
    return connection.execute(
        text(
            "SELECT id, name, base_url, auth_type, username, secret_envelope "
            "FROM connectionprofile WHERE kind = :kind AND state = 'ACTIVE' LIMIT 1"
        ),
        {"kind": kind},
    ).first()


def _f21_encrypt_env_token(token: str) -> tuple[str | None, str]:
    """Return ``(envelope_json, note)`` for a plaintext ``.env`` token.

    Without a usable master key the token is deliberately dropped rather than
    stored in the clear or made to fail the whole upgrade: the adopted source
    lands disabled anyway, so the user re-enters the credential before enabling.
    """
    if not token:
        return None, "no_credential"
    try:
        from app.config import settings as app_settings
        from app.crypto import SecretBox

        envelope = SecretBox(app_settings.master_key).encrypt(token)
    except Exception:  # noqa: BLE001 - a missing key must not block startup
        return None, "credential_needs_reentry"
    return json.dumps(envelope), "credential_migrated"


def _f21_adopted_id(prefix: str, base_url: str) -> str:
    digest = hashlib.sha256(f"f21:{base_url}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _f21_adopt_alertmanager(connection: Connection, now: datetime) -> dict[str, Any]:
    """Create one DISABLED Alertmanager source from the best available config."""
    from app.config import settings as app_settings

    profile = _f21_active_profile(connection, "ALERTMANAGER")
    if profile is not None:
        base_url = str(profile.base_url or "")
        auth_kind = str(profile.auth_type or "NONE")
        username = str(profile.username or "")
        envelope = (
            json.dumps(_decoded_json(profile.secret_envelope, None))
            if profile.secret_envelope is not None
            else None
        )
        note = "from_connection_profile"
        profile_id = int(profile.id)
        name = str(profile.name or "")[:120] or "Imported Alertmanager"
    else:
        base_url = str(app_settings.alertmanager_url or "")
        if not base_url:
            return {"adopted": False}
        envelope, note = _f21_encrypt_env_token(
            str(app_settings.alertmanager_token or "")
        )
        auth_kind = "BEARER" if envelope else "NONE"
        username = ""
        profile_id = None
        name = "Imported Alertmanager"

    source_id = _f21_adopted_id("src", base_url)
    poll_interval = max(5, min(int(app_settings.poll_interval_seconds), 3600))
    connection.execute(
        text(
            "INSERT OR IGNORE INTO eventsource "
            "(id, type, name, lifecycle_state, active_revision_id, created_at, "
            "updated_at, archived_at, version) "
            "VALUES (:id, 'ALERTMANAGER', :name, 'DISABLED', NULL, :now, :now, NULL, 1)"
        ),
        {"id": source_id, "name": name, "now": now},
    )
    result = connection.execute(
        text(
            "INSERT INTO eventsourcerevision "
            "(source_id, revision_no, internal_state, poll_interval_seconds, "
            "resolution_grace_seconds, max_parallel_endpoints, watchdog_enabled, "
            "watchdog_alertname, watchdog_identity_label, "
            "watchdog_missing_after_seconds, thanos_binding_id, last_test_status, "
            "tested_at, safe_error_code, legacy_connection_profile_id, created_at, "
            "activated_at) VALUES "
            "(:source_id, 1, 'PENDING', :poll_interval, :grace, 4, 0, 'Watchdog', "
            "'cluster', :missing_after, NULL, NULL, NULL, NULL, :profile_id, :now, NULL)"
        ),
        {
            "source_id": source_id,
            "poll_interval": poll_interval,
            "grace": max(0, min(int(app_settings.resolution_grace_seconds), 86400)),
            "missing_after": max(3 * poll_interval, 60),
            "profile_id": profile_id,
            "now": now,
        },
    )
    connection.execute(
        text(
            "INSERT INTO alertmanagerendpointrevision "
            "(source_revision_id, position, canonical_url, enabled, auth_kind, "
            "username, secret_envelope_json) VALUES "
            "(:revision_id, 0, :url, 1, :auth_kind, :username, :envelope)"
        ),
        {
            "revision_id": int(result.lastrowid),
            "url": canonical_alertmanager_url(base_url),
            "auth_kind": auth_kind,
            "username": username,
            "envelope": envelope,
        },
    )
    return {"adopted": True, "source_id": source_id, "note": note}


def _f21_adopt_thanos(connection: Connection, now: datetime) -> dict[str, Any]:
    """Create one DISABLED Thanos historical source carrying its connection."""
    from app.config import settings as app_settings

    profile = _f21_active_profile(connection, "THANOS")
    if profile is not None:
        base_url = str(profile.base_url or "")
        auth_kind = str(profile.auth_type or "NONE")
        username = str(profile.username or "")
        envelope = (
            json.dumps(_decoded_json(profile.secret_envelope, None))
            if profile.secret_envelope is not None
            else None
        )
        note = "from_connection_profile"
        profile_id = int(profile.id)
        name = str(profile.name or "")[:120] or "Imported Thanos"
    else:
        base_url = str(app_settings.thanos_url or "")
        if not base_url:
            return {"adopted": False}
        envelope, note = _f21_encrypt_env_token(str(app_settings.thanos_token or ""))
        auth_kind = "BEARER" if envelope else "NONE"
        username = ""
        profile_id = None
        name = "Imported Thanos"

    historical_id = _f21_adopted_id("hist", base_url)
    connection.execute(
        text(
            "INSERT OR IGNORE INTO historicaldatasource "
            "(id, type, name, lifecycle_state, active_revision_id, created_at, "
            "updated_at, archived_at, version) "
            "VALUES (:id, 'THANOS', :name, 'DISABLED', NULL, :now, :now, NULL, 1)"
        ),
        {"id": historical_id, "name": name, "now": now},
    )
    connection.execute(
        text(
            "INSERT INTO historicaldatasourcerevision "
            "(historical_source_id, revision_no, internal_state, canonical_url, "
            "auth_kind, username, secret_envelope_json, timeout_seconds, "
            "last_test_status, tested_at, safe_error_code, "
            "legacy_connection_profile_id, created_at, activated_at) VALUES "
            "(:historical_id, 1, 'PENDING', :url, :auth_kind, :username, :envelope, "
            ":timeout, NULL, NULL, NULL, :profile_id, :now, NULL)"
        ),
        {
            "historical_id": historical_id,
            "url": base_url.rstrip("/"),
            "auth_kind": auth_kind,
            "username": username,
            "envelope": envelope,
            "timeout": max(1, min(int(app_settings.thanos_timeout_seconds), 120)),
            "profile_id": profile_id,
            "now": now,
        },
    )
    return {"adopted": True, "historical_source_id": historical_id, "note": note}


def _f21_repoint_legacy_data(
    connection: Connection, target_source_id: str, now: datetime
) -> dict[str, int]:
    """Move Alert/Incident rows off legacy source ids onto the adopted source.

    Only ids without a non-archived EventSource are touched, which is what makes
    this idempotent: after the first run every row already points at the adopted
    source, so nothing matches.
    """
    moved_rows = 0
    moved_ids = 0
    # Audit is valuable but must never be able to fail the upgrade; a database
    # old enough to lack the table still deserves its data repointed.
    can_audit = inspect(connection).has_table("configaudit")
    for table in ("alert", "incident"):
        if not inspect(connection).has_table(table):
            continue
        stale = [
            str(row[0])
            for row in connection.execute(
                text(
                    f"SELECT DISTINCT source_id FROM {table} "
                    "WHERE source_id IS NOT NULL AND trim(source_id) != '' "
                    "AND source_id != :target AND source_id NOT IN "
                    "(SELECT id FROM eventsource WHERE lifecycle_state != 'ARCHIVED')"
                ),
                {"target": target_source_id},
            ).all()
        ]
        for source_id in sorted(stale):
            result = connection.execute(
                text(f"UPDATE {table} SET source_id = :target WHERE source_id = :old"),
                {"target": target_source_id, "old": source_id},
            )
            row_count = int(result.rowcount or 0)
            moved_rows += row_count
            moved_ids += 1
            if not can_audit:
                continue
            connection.execute(
                text(
                    "INSERT INTO configaudit "
                    "(resource_type, resource_id, action, actor, redacted_diff_json, "
                    "result, created_at) VALUES "
                    "('EVENT_SOURCE', :target, 'ADOPT_LEGACY_SOURCE', 'migration', "
                    ":diff, 'SUCCESS', :now)"
                ),
                {
                    "target": target_source_id,
                    "diff": json.dumps(
                        {
                            "table": table,
                            "from_source_id": source_id,
                            "to_source_id": target_source_id,
                            "rows": row_count,
                        }
                    ),
                    "now": now,
                },
            )
    return {"repointed_rows": moved_rows, "repointed_source_ids": moved_ids}


def _migration_6_f21_registry_only_configuration(
    connection: Connection,
) -> dict[str, int]:
    from app.f20_models import F20Model

    F20Model.metadata.create_all(connection)

    if _f21_registry_in_use(connection):
        return {
            "adopted_alertmanager": 0,
            "adopted_thanos": 0,
            "repointed_rows": 0,
            "repointed_source_ids": 0,
        }

    now = datetime.now(timezone.utc)
    alertmanager = _f21_adopt_alertmanager(connection, now)
    thanos = _f21_adopt_thanos(connection, now)

    repointed = {"repointed_rows": 0, "repointed_source_ids": 0}
    if alertmanager.get("adopted"):
        repointed = _f21_repoint_legacy_data(
            connection, str(alertmanager["source_id"]), now
        )

    return {
        "adopted_alertmanager": 1 if alertmanager.get("adopted") else 0,
        "adopted_thanos": 1 if thanos.get("adopted") else 0,
        **repointed,
    }


def _migration_7_f21_group_key_after_adoption(
    connection: Connection,
) -> dict[str, int]:
    """Recompute group keys after v6 repointed `source_id`.

    v6 moved historical Alert/Incident rows onto the adopted source but left
    `group_key` alone, and the key embeds the source id. Every startup then
    recomputed a key that matched nothing, emptied the old incidents, and tried
    to delete them -- which failed on the `superseded_by_incident_id` foreign
    key and took the whole application down before it could serve a request.

    Repointing also collapses two formerly distinct sources into one, so keys
    that used to differ now collide. That is exactly what migration 5 already
    solves, so this reuses its logic rather than inventing a second one:
    pick a canonical incident per key, move the others' alerts onto it and mark
    them superseded with a unique historical key.
    """
    return _migrate_f20_incidents(connection)


def _f22_audit(
    connection: Connection,
    source_id: str,
    action: str,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    if not inspect(connection).has_table("configaudit"):
        return
    connection.execute(
        text(
            "INSERT INTO configaudit "
            "(resource_type, resource_id, action, actor, redacted_diff_json, "
            "result, created_at) VALUES "
            "('EVENT_SOURCE', :target, :action, 'migration', :diff, 'SUCCESS', :now)"
        ),
        {
            "target": source_id,
            "action": action,
            "diff": json.dumps(payload),
            "now": now,
        },
    )


def _f22_collapse_configuration(connection: Connection, now: datetime) -> int:
    """Leave exactly one ACTIVE configuration row per source.

    ACTIVE wins over PENDING because it is the configuration that is actually
    running; an unapplied change is discarded with an audit trail rather than
    silently promoted into service.

    Superseded rows are retired, never deleted.  ``sourcepollrun``,
    ``endpointpollresult`` and ``alertendpointobservation`` all carry foreign
    keys into these two tables, and deleting a referenced row is exactly the
    class of mistake that took startup down after the F21 adoption.  A retired
    row is invisible: every read path selects ``internal_state = 'ACTIVE'``.
    """
    collapsed = 0
    # Archived rows include migration 5's placeholders for pre-F20 config. They
    # are read-only history with no configuration UI, so they keep whatever
    # shape they were left in.
    sources = connection.execute(
        text(
            "SELECT id FROM eventsource WHERE lifecycle_state != 'ARCHIVED' "
            "ORDER BY id"
        )
    ).all()
    for (source_id,) in sources:
        rows = connection.execute(
            text(
                "SELECT id, revision_no, internal_state FROM eventsourcerevision "
                "WHERE source_id = :source ORDER BY revision_no DESC, id DESC"
            ),
            {"source": source_id},
        ).all()
        if not rows:
            continue
        active = [row for row in rows if str(row[2]) == "ACTIVE"]
        pending = [row for row in rows if str(row[2]) == "PENDING"]
        keep = (active or pending or rows)[0]
        keep_id = int(keep[0])
        if active and pending:
            _f22_audit(
                connection,
                str(source_id),
                "F22_PENDING_DISCARDED",
                {"discarded_config_version": int(pending[0][1])},
                now,
            )
        for row in rows:
            if int(row[0]) == keep_id:
                continue
            connection.execute(
                text(
                    "UPDATE eventsourcerevision SET internal_state = 'RETIRED' "
                    "WHERE id = :id"
                ),
                {"id": int(row[0])},
            )
        connection.execute(
            text(
                "UPDATE eventsourcerevision "
                "SET internal_state = 'ACTIVE', "
                "activated_at = COALESCE(activated_at, :now) WHERE id = :id"
            ),
            {"id": keep_id, "now": now},
        )
        connection.execute(
            text(
                "UPDATE eventsource SET active_revision_id = :revision "
                "WHERE id = :source"
            ),
            {"revision": keep_id, "source": source_id},
        )
        collapsed += 1
    return collapsed


def _f22_thanos_row(connection: Connection, historical_id: str) -> Any:
    """Return the connection a Historical Data Source is actually using."""
    if not inspect(connection).has_table("historicaldatasourcerevision"):
        return None
    return connection.execute(
        text(
            "SELECT canonical_url, auth_kind, username, secret_envelope_json, "
            "timeout_seconds FROM historicaldatasourcerevision "
            "WHERE historical_source_id = :id "
            "ORDER BY CASE internal_state WHEN 'ACTIVE' THEN 0 "
            "WHEN 'PENDING' THEN 1 ELSE 2 END, revision_no DESC LIMIT 1"
        ),
        {"id": historical_id},
    ).first()


def _f22_absorb_thanos(connection: Connection, now: datetime) -> int:
    """Move each source's enabled Thanos binding onto the source itself.

    Ciphertext is carried over verbatim -- nothing is decrypted or
    re-encrypted -- so a database whose master key is missing still upgrades.
    """
    if not inspect(connection).has_table("sourceevidencebinding"):
        return 0
    absorbed = 0
    bindings = connection.execute(
        text(
            "SELECT id, source_id, historical_source_id FROM sourceevidencebinding "
            "WHERE enabled = 1 ORDER BY source_id, id"
        )
    ).all()
    seen: set[str] = set()
    for binding_id, source_id, historical_id in bindings:
        if str(source_id) in seen:
            _f22_audit(
                connection,
                str(source_id),
                "F22_BINDING_DROPPED",
                {"binding_id": int(binding_id)},
                now,
            )
            continue
        row = _f22_thanos_row(connection, str(historical_id))
        if row is None or not str(row[0] or ""):
            continue
        seen.add(str(source_id))
        connection.execute(
            text(
                "INSERT OR IGNORE INTO sourcethanosconfig "
                "(source_id, canonical_url, auth_kind, username, "
                "secret_envelope_json, timeout_seconds, updated_at) "
                "VALUES (:source, :url, :auth, :username, :secret, :timeout, :now)"
            ),
            {
                "source": source_id,
                "url": str(row[0]),
                "auth": str(row[1] or "NONE"),
                "username": str(row[2] or ""),
                "secret": row[3],
                "timeout": max(1, min(int(row[4] or 15), 120)),
                "now": now,
            },
        )
        absorbed += 1
    return absorbed


def _migration_8_f22_single_configuration(
    connection: Connection,
) -> dict[str, int]:
    from app.f20_models import F20Model, SourceThanosConfig

    F20Model.metadata.create_all(
        connection, tables=[SourceThanosConfig.__table__], checkfirst=True
    )
    now = datetime.now(timezone.utc)
    return {
        "collapsed_sources": _f22_collapse_configuration(connection, now),
        "absorbed_thanos": _f22_absorb_thanos(connection, now),
    }


def _f24_channel_config(row: Any) -> dict[str, Any]:
    """Assemble one Feishu revision's flat columns into a provider config blob.

    The five columns stay in place -- additive-only, and they are the only record
    of what a historical revision held. Runtime reads `config_envelope` from here
    on, so this has to be lossless for the fields the provider actually uses.
    """

    return {
        "kind": "FEISHU_CUSTOM_BOT",
        "webhook": _decoded_json(row.webhook_envelope, {}),
        "signing_secret": _decoded_json(row.signing_secret_envelope, None),
        "required_keyword": row.required_keyword,
        "mention_mode": row.mention_mode or "NONE",
        "mention_users": _decoded_json(row.mention_users, []),
        "mention_on": _decoded_json(row.mention_on, {}),
    }


def _f24_backfill_channel_config(connection: Connection) -> int:
    """Copy existing Feishu channel revisions into `config_envelope`.

    Idempotent: rows that already carry a non-empty envelope are skipped, so a
    repeated run (or a restart mid-upgrade) changes nothing.
    """

    if not inspect(connection).has_table("notificationchannelrevision"):
        return 0
    columns = _connection_column_names(connection, "notificationchannelrevision")
    if "config_envelope" not in columns:
        return 0
    rows = connection.execute(
        text(
            """
            SELECT id, provider, webhook_envelope, signing_secret_envelope,
                   required_keyword, mention_mode, mention_users, mention_on,
                   config_envelope
            FROM notificationchannelrevision
            """
        )
    ).all()
    migrated = 0
    for row in rows:
        existing = _decoded_json(row.config_envelope, None)
        if existing:
            continue
        payload = _f24_channel_config(row)
        connection.execute(
            text(
                "UPDATE notificationchannelrevision SET config_envelope = :payload,"
                " provider = :provider WHERE id = :id"
            ),
            {
                "payload": json.dumps(payload, ensure_ascii=False),
                "provider": row.provider or "FEISHU_CUSTOM_BOT",
                "id": row.id,
            },
        )
        migrated += 1
    return migrated


def _migration_9_f24_provider_config(connection: Connection) -> dict[str, int]:
    _add_connection_column(
        connection, "notificationchannelrevision", "config_envelope", "JSON"
    )
    return {"channel_configs": _f24_backfill_channel_config(connection)}


def _migration_10_f26_occurrence_history(connection: Connection) -> dict[str, int]:
    """Create the append-only occurrence history table. Nothing is backfilled.

    Deliberately no synthesis of past occurrences (ADR 0008 / design D12): for a
    group that is already `recovered` at upgrade time we do not know *when* it
    recovered -- `Incident.updated_at` is "last change of anything", and
    `occurrence_started_at` has been overwritten by every recurrence so far.
    Inventing a `recovered_at` would break the single property that makes an
    append-only history worth keeping. The page starts empty and says so.
    """
    from app.f20_models import F20Model, IncidentOccurrence

    F20Model.metadata.create_all(
        connection, tables=[IncidentOccurrence.__table__], checkfirst=True
    )
    return {"backfilled_occurrences": 0}


def _migration_13_f27_model_call_log(connection: Connection) -> dict[str, int]:
    """Record every outbound model call, so a paid action stops being invisible.

    Found by a review of F27, not by a failure: `POST /suggested-queries` spent
    money and wrote nothing, so "how often did I use this and what did it cost"
    had no answer, and a double-click was two bills held back only by a disabled
    button in the browser.

    A new table rather than a column anywhere: v11 froze four model classes and
    v12 already had to work around that once. Additive only, always.

    The scope column holds `source_id@version` rather than a bare id, because a
    stored run is only reusable while the configuration it was checked against
    is still in use (D38).
    """
    from app.f20_models import F20Model, ModelCallLog

    F20Model.metadata.create_all(
        connection, tables=[ModelCallLog.__table__], checkfirst=True
    )
    return {"logged_calls": 0}


def _migration_14_grafana_judgment_baselines(
    connection: Connection,
) -> dict[str, int]:
    """Create the four tables behind Grafana import and reference baselines.

    Two of them are obviously *about* `MetricQueryTemplate` and would be columns
    on it in any schema written from scratch. They cannot be: v11 lists that
    class among its checksum dependencies, so adding a field would change an
    applied migration's checksum and every existing database would refuse to
    start. v12 already paid this once for `MetricTemplateExtras`; this is the
    same price for the same reason, and merging them later is not an option.

    **All four at once, including `DeliveryEvidenceSnapshot`, which no code
    reads yet.** Deferring it to a later version would fork the schema rather
    than defer anything: migration 5 calls `create_all(connection)` with no
    `tables=` argument, so a *fresh* database gets every table on this MetaData
    immediately, while an upgraded one would wait. An additive empty table costs
    nothing; two different meanings of "current schema" cost a great deal.

    Nothing is backfilled. A baseline is a value only the user can supply — a
    synthesised one would be an invented judgment standard, which is precisely
    what this capability exists to replace.
    """
    from app.f20_models import (
        DeliveryEvidenceSnapshot,
        F20Model,
        MetricTemplateBaseline,
        MetricTemplateOrigin,
        SourceGrafanaConfig,
    )

    F20Model.metadata.create_all(
        connection,
        tables=[
            SourceGrafanaConfig.__table__,
            MetricTemplateBaseline.__table__,
            MetricTemplateOrigin.__table__,
            DeliveryEvidenceSnapshot.__table__,
        ],
        checkfirst=True,
    )
    return {"backfilled_baselines": 0}


def _migration_15_metric_template_source_scope(
    connection: Connection,
) -> dict[str, int]:
    """Give frozen metric templates the existing ALL/SELECTED Source Scope.

    Imported templates are source knowledge: letting one imported from source A
    silently run against source B would apply both its query and its judgment
    baseline in a context the user never approved.  Existing imported rows are
    therefore backfilled to their Origin source.  Hand-written templates retain
    the historical ALL default by having no side row.
    """
    from app.f20_models import F20Model, MetricTemplateSourceScope

    F20Model.metadata.create_all(
        connection, tables=[MetricTemplateSourceScope.__table__], checkfirst=True
    )
    existing = {
        int(row[0])
        for row in connection.execute(
            text("SELECT template_id FROM metrictemplatesourcescope")
        ).all()
    }
    origins = connection.execute(
        text("SELECT template_id, source_id FROM metrictemplateorigin")
    ).all()
    written = 0
    for template_id, source_id in origins:
        template_id = int(template_id)
        if template_id in existing:
            continue
        connection.execute(
            text(
                "INSERT INTO metrictemplatesourcescope "
                "(template_id, scope_mode, source_ids_json, created_at, updated_at) "
                "VALUES (:template_id, 'SELECTED', :source_ids_json, :now, :now)"
            ),
            {
                "template_id": template_id,
                "source_ids_json": json.dumps([str(source_id)]),
                "now": datetime.now(timezone.utc),
            },
        )
        existing.add(template_id)
        written += 1
    return {"backfilled_template_scopes": written}


def _migration_12_f27_template_extras(connection: Connection) -> dict[str, int]:
    """Add template ordering in a **new table**, because v11 froze the old one.

    The design review found two gaps after v11 had already shipped: auxiliary
    curves were ordered by name and silently truncated at the cap, and a ratio
    charted without a unit reads as a raw count. Both belong on the template —
    but `MetricQueryTemplate` is inside migration 11's checksum, so touching it
    would stop every existing database from starting.

    Nothing is backfilled: a template with no row here takes the defaults, which
    is exactly the behaviour it had before this migration.
    """
    from app.f20_models import F20Model, MetricTemplateExtras

    F20Model.metadata.create_all(
        connection, tables=[MetricTemplateExtras.__table__], checkfirst=True
    )
    return {"backfilled_extras": 0}


def _migration_11_f27_metric_evidence(connection: Connection) -> dict[str, int]:
    """Create the F27 tables. Schema only -- no rows are written here.

    The built-in templates are **not** seeded by this migration. Their PromQL
    will need revising as it meets real clusters, and a migration is exactly the
    wrong place for data that has to keep changing: its checksum is frozen the
    moment it is applied anywhere. Seeding happens idempotently at startup, where
    a later version can correct a query without touching an applied migration.

    Both stages' tables land in one migration on purpose: splitting them would
    give databases that stopped between stage 1 and stage 2 an extra backup and
    ledger entry for no benefit.
    """
    from app.f20_models import (
        F20Model,
        Investigation,
        MetricQueryTemplate,
        ModelChannel,
        ModelChannelRevision,
    )

    F20Model.metadata.create_all(
        connection,
        tables=[
            MetricQueryTemplate.__table__,
            ModelChannel.__table__,
            ModelChannelRevision.__table__,
            Investigation.__table__,
        ],
        checkfirst=True,
    )
    return {"seeded_templates": 0}


MIGRATIONS = (
    Migration(
        1,
        "m1_source_compatibility",
        "alert missing/source/upstream; incident source; source indexes",
        _migration_1_m1_compatibility,
    ),
    Migration(
        2,
        "f17_notification_foundation",
        "incident route fields/occurrence plus connection/channel/policy/route/outbox tables",
        _migration_2_f17_foundation,
    ),
    Migration(
        3,
        "f17_runtime_settings",
        "non-secret Web runtime settings and migration flags",
        _migration_3_runtime_settings,
    ),
    Migration(
        4,
        "f17_delivery_manual_trigger",
        "delivery next attempt trigger for audited manual retry",
        _migration_4_delivery_manual_trigger,
    ),
    Migration(
        5,
        "f20_event_source_foundation",
        "stable EventSource revisions/endpoints, poll/watchdog/scope/binding tables, incident group key v2",
        _migration_5_f20_event_source_foundation,
        transactional=True,
        checksum_dependencies=(
            EventSource,
            EventSourceRevision,
            AlertmanagerEndpointRevision,
            SourcePollRun,
            EndpointPollResult,
            AlertEndpointObservation,
            MonitoredCluster,
            AggregationRuleSource,
            NotificationPolicySource,
            HistoricalDataSource,
            SourceEvidenceBinding,
            build_group_key_v2,
            canonical_alertmanager_url,
            source_id_for_alertmanager,
            _connection_column_names,
            _add_connection_column,
            _decoded_json,
            _profile_source_id,
            _safe_source_name,
            _migration_datetime_key,
            _f20_incident_key,
            _migrate_f20_incidents,
        ),
    ),
    Migration(
        6,
        "f21_registry_only_configuration",
        "historical data source revisions plus one-time adoption of env/profile config",
        _migration_6_f21_registry_only_configuration,
        transactional=True,
        checksum_dependencies=(
            HistoricalDataSourceRevision,
            _f21_registry_in_use,
            _f21_active_profile,
            _f21_encrypt_env_token,
            _f21_adopted_id,
            _f21_adopt_alertmanager,
            _f21_adopt_thanos,
            _f21_repoint_legacy_data,
        ),
    ),
    Migration(
        7,
        "f21_group_key_after_adoption",
        "recompute incident group keys and collisions after adoption repointing",
        _migration_7_f21_group_key_after_adoption,
        transactional=True,
        checksum_dependencies=(
            _migrate_f20_incidents,
            _f20_incident_key,
            build_group_key_v2,
        ),
    ),
    Migration(
        8,
        "f22_single_configuration",
        "one ACTIVE configuration row per source plus Thanos absorbed onto it",
        _migration_8_f22_single_configuration,
        transactional=True,
        checksum_dependencies=(
            SourceThanosConfig,
            _f22_audit,
            _f22_collapse_configuration,
            _f22_thanos_row,
            _f22_absorb_thanos,
        ),
    ),
    Migration(
        9,
        "f24_provider_config",
        "per-provider channel config envelope; existing Feishu revisions copied in",
        _migration_9_f24_provider_config,
        transactional=True,
        checksum_dependencies=(
            _f24_channel_config,
            _f24_backfill_channel_config,
        ),
    ),
    Migration(
        10,
        "f26_occurrence_history",
        "append-only incident occurrence history table; nothing backfilled",
        _migration_10_f26_occurrence_history,
        transactional=True,
        # Listing the model freezes its source: editing the class from here on
        # changes this applied migration's checksum and every existing database
        # refuses to start. Add a new table instead of a field.
        checksum_dependencies=(IncidentOccurrence,),
    ),
    Migration(
        11,
        "f27_metric_evidence_and_investigations",
        "metric query templates, model channels and investigation records",
        _migration_11_f27_metric_evidence,
        transactional=True,
        # Same freeze as above: these four classes may not gain fields after this
        # migration is applied anywhere. Add a new table instead.
        checksum_dependencies=(
            MetricQueryTemplate,
            ModelChannel,
            ModelChannelRevision,
            Investigation,
        ),
    ),
    Migration(
        12,
        "f27_metric_template_extras",
        "explicit priority and display unit for metric templates",
        _migration_12_f27_template_extras,
        transactional=True,
        checksum_dependencies=(MetricTemplateExtras,),
    ),
    Migration(
        13,
        "f27_model_call_log",
        "audit and idempotency for outbound model calls",
        _migration_13_f27_model_call_log,
        transactional=True,
        checksum_dependencies=(ModelCallLog,),
    ),
    Migration(
        14,
        "grafana_judgment_baselines",
        "per-source Grafana address, imported template origins, reference "
        "baselines and the delivery evidence snapshot",
        _migration_14_grafana_judgment_baselines,
        transactional=True,
        # Listing the models freezes their source: from here on none of these
        # four classes may gain, lose or rename a field, because that would
        # change this applied migration's checksum and stop every existing
        # database from starting. Add a new table instead.
        checksum_dependencies=(
            SourceGrafanaConfig,
            MetricTemplateBaseline,
            MetricTemplateOrigin,
            DeliveryEvidenceSnapshot,
        ),
    ),
    Migration(
        15,
        "metric_template_source_scope",
        "Source Scope for frozen metric query templates",
        _migration_15_metric_template_source_scope,
        transactional=True,
        checksum_dependencies=(MetricTemplateSourceScope,),
    ),
)


def _ensure_ledger(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schemamigration (
                    version INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    checksum VARCHAR NOT NULL,
                    applied_at DATETIME NOT NULL
                )
                """
            )
        )


def _applied(engine: Engine) -> dict[int, tuple[str, str]]:
    _ensure_ledger(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT version, name, checksum FROM schemamigration ORDER BY version")
        ).all()
    return {int(row[0]): (str(row[1]), str(row[2])) for row in rows}


def _database_path(engine: Engine, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    value = engine.url.database
    if not value or value == ":memory:":
        return None
    return Path(value)


def _backup_once(database_path: Path, *, label: str = "f17") -> Path:
    existing = sorted(
        database_path.parent.glob(f"{database_path.name}.pre-{label}-*.bak")
    )
    if existing:
        return existing[0]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = database_path.with_name(
        f"{database_path.name}.pre-{label}-{timestamp}.bak"
    )
    with sqlite3.connect(database_path) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def _is_fresh_database(engine: Engine) -> bool:
    tables = {
        name
        for name in inspect(engine).get_table_names()
        if name != "schemamigration" and not name.startswith("sqlite_")
    }
    return not tables


def run_migrations(
    engine: Engine, *, database_path: Path | None = None
) -> MigrationReport:
    """Apply verified migrations before scheduler/worker startup."""

    from app import models  # noqa: F401 - register latest metadata

    fresh = _is_fresh_database(engine)
    path = _database_path(engine, database_path)
    had_ledger = inspect(engine).has_table("schemamigration")
    backup_path: Path | None = None
    if not fresh and not had_ledger and path is not None and path.exists():
        # The first write to an unversioned legacy database must happen only
        # after its recoverable snapshot exists.
        backup_path = _backup_once(path, label="f17")
    if fresh:
        SQLModel.metadata.create_all(engine)

    applied = _applied(engine)
    for migration in MIGRATIONS:
        previous = applied.get(migration.version)
        if previous is not None and previous[1] != migration.checksum:
            raise MigrationChecksumError(
                f"migration {migration.version} checksum mismatch; "
                "refusing to guess schema state"
            )

    pending = [item for item in MIGRATIONS if item.version not in applied]
    if (
        pending
        and not fresh
        and backup_path is None
        and path is not None
        and path.exists()
    ):
        # One backup per upgrade wave, keyed by the lowest pending version.
        # The label used to be one of two fixed buckets, so once a pre-f20
        # backup existed every later migration reused it and ran unprotected --
        # v6 and v7 did exactly that. Keep the historical labels readable.
        lowest = min(item.version for item in pending)
        label = "f17" if lowest < 5 else "f20" if lowest == 5 else f"v{lowest}"
        backup_path = _backup_once(path, label=label)
    elif backup_path is None and path is not None:
        backups = sorted(path.parent.glob(f"{path.name}.pre-*.bak"))
        backup_path = backups[-1] if backups else None

    applied_versions: list[int] = []
    details: dict[int, dict[str, int]] = {}
    for migration in pending:
        try:
            if migration.transactional:
                with engine.connect() as connection:
                    # Pysqlite otherwise defers BEGIN until the first DML and may
                    # autocommit preceding DDL.  An explicit BEGIN keeps F20
                    # CREATE/ALTER/data changes and its ledger row atomic.
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    try:
                        migration_details = migration.apply(connection) or {}
                        connection.execute(
                            text(
                                "INSERT INTO schemamigration "
                                "(version, name, checksum, applied_at) "
                                "VALUES (:version, :name, :checksum, :applied_at)"
                            ),
                            {
                                "version": migration.version,
                                "name": migration.name,
                                "checksum": migration.checksum,
                                "applied_at": datetime.now(timezone.utc),
                            },
                        )
                    except Exception:
                        connection.rollback()
                        raise
                    else:
                        connection.commit()
            else:
                migration_details = migration.apply(engine) or {}
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO schemamigration "
                            "(version, name, checksum, applied_at) "
                            "VALUES (:version, :name, :checksum, :applied_at)"
                        ),
                        {
                            "version": migration.version,
                            "name": migration.name,
                            "checksum": migration.checksum,
                            "applied_at": datetime.now(timezone.utc),
                        },
                    )
        except Exception as exc:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed; "
                f"backup={backup_path or 'not-required'}"
            ) from exc
        applied_versions.append(migration.version)
        if migration_details:
            details[migration.version] = dict(sorted(migration_details.items()))

    return MigrationReport(
        applied_versions=applied_versions,
        backup_path=backup_path,
        details=details,
    )
