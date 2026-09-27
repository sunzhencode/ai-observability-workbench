"""Service Catalog, Mapping publication and manual assignment HTTP contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.application.sources import EndpointDraft, SourceDraft
from app.bootstrap import create_job_platform_app
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations


UTC = timezone.utc


def _candidate(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "service-catalog-api.db",
        master_key_path=key,
        cursor_secret=b"catalog-api-cursor-key-at-least-32-bytes",
    )
    resources.sources.create_source(
        SourceDraft(
            "src-a",
            "Primary AM",
            (EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return resources


def _apply(resources, now: datetime) -> None:
    alert = {
        "fingerprint": "checkout-down",
        "labels": {
            "alertname": "CheckoutDown",
            "severity": "critical",
            "cluster": "cluster-a",
            "service": "checkout",
        },
        "annotations": {"summary": "checkout unavailable"},
        "startsAt": "2026-08-24T00:00:00Z",
    }
    snapshot = resources.sources.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", (alert,), 1),)
    )
    assert resources.sources.apply_collection(
        snapshot, outcome, observed_at=now
    ).committed


def test_service_mapping_and_manual_assignment_api(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        created = client.post(
            "/api/v1/services",
            headers={"Idempotency-Key": "create-checkout-service"},
            json={
                "name": "Checkout API",
                "slug": "checkout-api",
                "criticality": "TIER_0",
                "links": ["https://runbooks.invalid/checkout"],
            },
        )
        assert created.status_code == 201
        checkout = created.json()
        assert checkout["ack_sla_seconds"] == 300

        manual_created = client.post(
            "/api/v1/services",
            headers={"Idempotency-Key": "create-storefront-service"},
            json={
                "name": "Storefront",
                "slug": "storefront",
                "criticality": "TIER_3",
                "links": [],
            },
        )
        assert manual_created.status_code == 201
        storefront = manual_created.json()

        rule_created = client.post(
            "/api/v1/service-mapping-rules",
            headers={"Idempotency-Key": "create-checkout-mapping"},
            json={
                "name": "Checkout alerts",
                "priority": 10,
                "service_id": checkout["id"],
                "enabled": True,
                "source_ids": ["src-a"],
                "matchers": [
                    {"label": "service", "operator": "=", "value": "checkout"}
                ],
            },
        )
        assert rule_created.status_code == 201
        rule = rule_created.json()
        assert rule["published_version"] == 0
        assert rule["has_unpublished_changes"] is True

        preview = client.post(
            f"/api/v1/service-mapping-rules/{rule['id']}/preview"
        )
        assert preview.status_code == 200
        assert preview.json()["matched_alert_count"] == 0

        published = client.post(
            f"/api/v1/service-mapping-rules/{rule['id']}/publish",
            json={"expected_version": rule["version"]},
        )
        assert published.status_code == 202
        assert published.json()["rule"]["has_unpublished_changes"] is False
        assert published.json()["reprojection_job"]["kind"] == "service-mapping.reproject"

        unchanged = client.post(
            f"/api/v1/service-mapping-rules/{rule['id']}/publish",
            json={"expected_version": published.json()["rule"]["version"]},
        )
        assert unchanged.status_code == 422
        assert unchanged.json()["error"]["code"] == "SERVICE_MAPPING_NO_CHANGES"

        detected = datetime(2026, 8, 24, 1, tzinfo=UTC)
        _apply(resources, detected)
        queue = client.get("/api/v1/occurrences")
        assert queue.status_code == 200
        occurrence = queue.json()["items"][0]
        assert occurrence["service_name"] == "Checkout API"
        assert occurrence["service_assignment_state"] == "MAPPED"
        assert occurrence["ack_sla_seconds"] == 300

        assigned = client.put(
            f"/api/v1/occurrences/{occurrence['id']}/service",
            headers={"Idempotency-Key": "assign-storefront-manually"},
            json={
                "service_id": storefront["id"],
                "expected_version": occurrence["version"],
            },
        )
        assert assigned.status_code == 200
        assert assigned.json()["assignment_origin"] == "MANUAL"
        assert assigned.json()["timeline"]["event_type"] == "SERVICE_ASSIGNMENT_CHANGED"

        overview = client.get(f"/api/v1/occurrences/{occurrence['id']}")
        assert overview.status_code == 200
        assert overview.json()["service_name"] == "Storefront"
        assert overview.json()["ack_sla_seconds"] == 300

        archived = client.post(
            f"/api/v1/services/{storefront['id']}/archive",
            json={"expected_version": storefront["version"]},
        )
        assert archived.status_code == 200
        archived_overview = client.get(f"/api/v1/occurrences/{occurrence['id']}")
        assert archived_overview.json()["service_name"] == "Storefront"
        assert (
            archived_overview.json()["service_assignment_state"]
            == "SERVICE_ARCHIVED"
        )

        audit = client.get(f"/api/v1/services/{storefront['id']}/audit")
        assert audit.status_code == 200
        assert [item["action"] for item in audit.json()] == ["CREATED", "ARCHIVED"]

        assert "/api/v1/services" in client.get("/openapi.json").json()["paths"]
        assert "owner" not in client.get("/api/v1/services").text.lower()
    resources.engine.dispose()
