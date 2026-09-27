"""Read-only, secret-free inventory of configuration that must be recreated."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Any


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _objects(
    connection: sqlite3.Connection,
    *,
    required_table: str,
    query: str,
) -> list[dict[str, Any]]:
    if not _table_exists(connection, required_table):
        return []
    return [dict(row) for row in connection.execute(query)]


def build_configuration_inventory(
    database_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect only allow-listed non-secret columns from a legacy database."""
    database = Path(database_path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError("configuration inventory database does not exist")
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        event_sources = _objects(
            connection,
            required_table="eventsource",
            query="""
                SELECT source.id, source.name, source.lifecycle_state,
                       source.version,
                       (SELECT COUNT(*)
                          FROM alertmanagerendpointrevision endpoint
                         WHERE endpoint.source_revision_id = revision.id) AS endpoint_count,
                       EXISTS(SELECT 1 FROM sourcethanosconfig thanos
                               WHERE thanos.source_id = source.id) AS thanos_configured,
                       EXISTS(SELECT 1 FROM sourcegrafanaconfig grafana
                               WHERE grafana.source_id = source.id) AS grafana_configured
                  FROM eventsource source
             LEFT JOIN eventsourcerevision revision
                    ON revision.id = COALESCE(
                        source.active_revision_id,
                        (SELECT MAX(candidate.id)
                           FROM eventsourcerevision candidate
                          WHERE candidate.source_id = source.id)
                    )
              ORDER BY source.id
            """,
        )
        aggregation_rules = _objects(
            connection,
            required_table="aggregationrule",
            query="""
                SELECT rule.id, rule.name, rule.priority, rule.scope_mode,
                       rule.enabled, rule.version,
                       (SELECT COUNT(*) FROM aggregationrulesource scope
                         WHERE scope.rule_id = rule.id) AS source_count
                  FROM aggregationrule rule
              ORDER BY rule.priority, rule.id
            """,
        )
        metric_templates = _objects(
            connection,
            required_table="metricquerytemplate",
            query="""
                SELECT template.id, template.name, template.builtin_key,
                       template.user_modified, template.enabled,
                       CASE WHEN EXISTS(
                           SELECT 1 FROM metrictemplateorigin origin
                            WHERE origin.template_id = template.id
                       ) THEN 'GRAFANA' ELSE 'MANUAL' END AS origin,
                       COALESCE(scope.scope_mode, 'ALL') AS scope_mode
                  FROM metricquerytemplate template
             LEFT JOIN metrictemplatesourcescope scope
                    ON scope.template_id = template.id
              ORDER BY template.id
            """,
        )
        model_channels = _objects(
            connection,
            required_table="modelchannel",
            query="""
                SELECT channel.id, channel.name, channel.kind, channel.enabled,
                       revision.state AS revision_state
                  FROM modelchannel channel
             LEFT JOIN modelchannelrevision revision
                    ON revision.id = (
                        SELECT MAX(candidate.id)
                          FROM modelchannelrevision candidate
                         WHERE candidate.channel_id = channel.id
                    )
              ORDER BY channel.id
            """,
        )
        notification_channels = _objects(
            connection,
            required_table="notificationchannel",
            query="""
                SELECT channel.id, channel.name, channel.state,
                       revision.provider
                  FROM notificationchannel channel
             LEFT JOIN notificationchannelrevision revision
                    ON revision.id = COALESCE(
                        channel.active_revision_id,
                        (SELECT MAX(candidate.id)
                           FROM notificationchannelrevision candidate
                          WHERE candidate.channel_id = channel.id)
                    )
              ORDER BY channel.id
            """,
        )
        notification_policies = _objects(
            connection,
            required_table="notificationpolicyrevision",
            query="""
                SELECT revision.logical_id, revision.version, revision.name,
                       revision.state, revision.priority, revision.scope_mode,
                       (SELECT COUNT(*) FROM notificationpolicysource scope
                         WHERE scope.policy_revision_id = revision.id) AS source_count,
                       (SELECT COUNT(*) FROM notificationpolicychannel target
                         WHERE target.policy_revision_id = revision.id) AS channel_count
                  FROM notificationpolicyrevision revision
                 WHERE revision.id = (
                       SELECT MAX(candidate.id)
                         FROM notificationpolicyrevision candidate
                        WHERE candidate.logical_id = revision.logical_id
                 )
              ORDER BY revision.priority, revision.logical_id
            """,
        )
        workbench_url_configured = False
        if _table_exists(connection, "runtimesetting"):
            workbench_url_configured = (
                connection.execute(
                    "SELECT 1 FROM runtimesetting WHERE key = 'workbench_url'"
                ).fetchone()
                is not None
            )

    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "database_name": database.name,
        "secrets_included": False,
        "objects": {
            "event_sources": event_sources,
            "aggregation_rules": aggregation_rules,
            "metric_templates": metric_templates,
            "model_channels": model_channels,
            "notification_channels": notification_channels,
            "notification_policies": notification_policies,
            "workbench_url": {"configured": workbench_url_configured},
        },
        "excluded_fields": [
            "endpoint and service URLs",
            "usernames",
            "authentication and signing secrets",
            "model configuration envelopes",
            "PromQL and matchers",
            "notification recipients",
            "workbench URL value",
        ],
    }
