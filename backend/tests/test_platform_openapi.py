"""Pin the platform API contract used to generate the typed frontend client."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.bootstrap import create_job_platform_app

SNAPSHOT = Path(__file__).parent / "platform_openapi_snapshot.json"


def _dump(schema: dict[str, object]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_platform_openapi_contract_matches_generated_client_source(tmp_path: Path) -> None:
    key = tmp_path / "master.key"
    key.write_text("test-only-existing-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"snapshot-cursor-key-at-least-32-bytes",
    )
    current = json.loads(json.dumps(resources.app.openapi(), sort_keys=True))
    resources.engine.dispose()

    if os.environ.get("PLATFORM_UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(_dump(current), encoding="utf-8")
        pytest.skip("platform OpenAPI snapshot refreshed")

    assert SNAPSHOT.exists(), (
        "missing platform snapshot; refresh with PLATFORM_UPDATE_OPENAPI_SNAPSHOT=1"
    )
    assert current == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(current["paths"]) == {
        "/api/v1/aggregation-rules",
        "/api/v1/aggregation-labels",
        "/api/v1/aggregation-rules/preview",
        "/api/v1/aggregation-rules/{rule_id}",
        "/api/v1/alerts",
        "/api/v1/alerts/{alert_id}/metric-evidence",
        "/api/v1/analytics/overview",
        "/api/v1/events",
        "/api/v1/events/poll",
        "/api/v1/incidents",
        "/api/v1/incidents/{incident_id}",
        "/api/v1/occurrences",
        "/api/v1/occurrences/batch-start-handling",
        "/api/v1/occurrences/{occurrence_id}",
            "/api/v1/occurrences/{occurrence_id}/investigations",
            "/api/v1/occurrences/{occurrence_id}/investigator-runs",
        "/api/v1/occurrences/{occurrence_id}/start-handling",
        "/api/v1/occurrences/{occurrence_id}/resolve",
        "/api/v1/occurrences/{occurrence_id}/timeline",
        "/api/v1/occurrences/{occurrence_id}/tasks",
        "/api/v1/occurrences/{occurrence_id}/tasks/{task_id}/transition",
        "/api/v1/occurrences/{occurrence_id}/notes",
        "/api/v1/occurrences/{occurrence_id}/notes/{sequence}/redact",
        "/api/v1/occurrences/{occurrence_id}/service",
        "/api/v1/occurrences/{occurrence_id}/similar",
        "/api/v1/occurrences/{occurrence_id}/noise",
        "/api/v1/occurrences/{occurrence_id}/suppression",
        "/api/v1/occurrences/{occurrence_id}/suppression/end",
        "/api/v1/incident-occurrences",
        "/api/v1/incident-occurrences/{occurrence_id}",
        "/api/v1/jobs/{job_id}",
        "/api/v1/investigation-usage/daily",
        "/api/v1/investigations/{investigation_id}",
        "/api/v1/investigations/{investigation_id}/cancel",
            "/api/v1/investigations/{investigation_id}/feedback",
            "/api/v1/investigator-runs/{investigation_id}",
            "/api/v1/investigator-runs/{investigation_id}/cancel",
            "/api/v1/investigator-runs/{investigation_id}/feedback",
        "/api/v1/metric-templates",
        "/api/v1/metric-templates/{template_id}",
        "/api/v1/model-channels",
        "/api/v1/model-channels/{channel_id}/activate",
        "/api/v1/model-channels/{channel_id}/disable",
        "/api/v1/model-channels/{channel_id}/draft",
        "/api/v1/model-channels/{channel_id}/enable",
        "/api/v1/model-channels/{channel_id}/models",
        "/api/v1/model-channels/{channel_id}/test",
        "/api/v1/model-runtime",
        "/api/v1/model-vendors",
        "/api/v1/platform-health",
        "/api/v1/prompt-profiles",
        "/api/v1/prompt-profiles/copy-standard",
        "/api/v1/prompt-profiles/{profile_id}/activate",
        "/api/v1/prompt-profiles/{profile_id}/draft",
        "/api/v1/prompt-profiles/{profile_id}/preview",
        "/api/v1/prompt-profiles/{profile_id}/revisions",
        "/api/v1/prompt-profiles/{profile_id}/test",
        "/api/v1/notification-channels",
        "/api/v1/notification-channels/{channel_id}/activate",
        "/api/v1/notification-channels/{channel_id}/disable",
        "/api/v1/notification-channels/{channel_id}/draft",
        "/api/v1/notification-channels/{channel_id}/enable",
        "/api/v1/notification-channels/{channel_id}/test",
        "/api/v1/notification-deliveries",
        "/api/v1/notification-deliveries/{delivery_id}",
        "/api/v1/notification-deliveries/{delivery_id}/retry",
        "/api/v1/notification-policies",
        "/api/v1/notification-policies/preview",
        "/api/v1/notification-policies/{logical_id}/draft",
        "/api/v1/notification-policies/{revision_id}/activate",
        "/api/v1/notification-policies/{revision_id}/disable",
        "/api/v1/notification-policies/{revision_id}/prepare-activation",
        "/api/v1/incidents/{incident_id}/notification",
        "/api/v1/sources",
        "/api/v1/sources/{source_id}",
        "/api/v1/sources/{source_id}/audit",
        "/api/v1/sources/{source_id}/noise-controls",
        "/api/v1/sources/{source_id}/archive",
        "/api/v1/sources/{source_id}/collect",
        "/api/v1/sources/{source_id}/disable",
        "/api/v1/sources/{source_id}/enable",
        "/api/v1/sources/{source_id}/grafana/dashboards",
        "/api/v1/sources/{source_id}/grafana/import/confirm",
        "/api/v1/sources/{source_id}/grafana/import/preview",
        "/api/v1/sources/{source_id}/monitoring/{kind}",
        "/api/v1/sources/{source_id}/monitoring",
        "/api/v1/sources/{source_id}/monitoring/{kind}/test",
        "/api/v1/sources/{source_id}/test",
        "/api/v1/sources/{source_id}/watchdog-clusters",
        "/api/v1/sources/{source_id}/watchdog-clusters/{identity_value}",
        "/api/v1/settings/workbench-url",
        "/api/v1/service-mapping-rules",
        "/api/v1/service-mapping-rules/{rule_id}",
        "/api/v1/service-mapping-rules/{rule_id}/preview",
        "/api/v1/service-mapping-rules/{rule_id}/publish",
        "/api/v1/services",
        "/api/v1/services/{service_id}",
        "/api/v1/services/{service_id}/archive",
        "/api/v1/services/{service_id}/audit",
        "/api/v1/maintenance-windows",
        "/api/v1/maintenance-windows/{maintenance_id}/end",
    }
    assert "ErrorEnvelope" in current["components"]["schemas"]
