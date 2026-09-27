"""Source-alerting API, Job wiring and bounded monitoring-adapter contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from fastapi.testclient import TestClient
import httpx

from app.adapters.monitoring.alertmanager import BoundedAlertmanagerReader
from app.application.sources import EndpointDraft, SourceDraft
from app.bootstrap import create_job_platform_app
from app.domains.sources.models import EndpointObservation, EndpointSnapshot

UTC = timezone.utc


class CandidateReader:
    async def fetch(self, endpoint: EndpointSnapshot) -> EndpointObservation:
        alerts: tuple[dict[str, object], ...] = ()
        if endpoint.position == 0:
            alerts = (
                {
                    "fingerprint": "target-one",
                    "labels": {
                        "alertname": "TargetDown",
                        "severity": "warning",
                        "cluster": "cluster-a",
                    },
                    "annotations": {"summary": "target down"},
                    "startsAt": "2026-08-11T00:00:00Z",
                },
                {
                    "fingerprint": "watchdog-one",
                    "labels": {
                        "alertname": "Watchdog",
                        "severity": "info",
                        "cluster": "cluster-a",
                    },
                    "annotations": {},
                    "startsAt": "2026-08-11T00:00:00Z",
                },
            )
        return EndpointObservation(endpoint, "SUCCESS", alerts, 2)


def _candidate(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"candidate-cursor-key-at-least-32-bytes",
        source_reader=CandidateReader(),
    )
    resources.sources.create_source(
        SourceDraft(
            id="src-a",
            name="Primary AM",
            endpoints=(
                EndpointDraft(0, "https://am-a.invalid"),
                EndpointDraft(1, "https://am-b.invalid"),
            ),
            watchdog_enabled=True,
        ),
        now=datetime(2026, 8, 11, tzinfo=UTC),
    )
    return resources


def test_candidate_collect_job_exposes_source_alert_incident_and_rule_api(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        source = client.get("/api/v1/sources/src-a")
        assert source.status_code == 200
        assert source.json()["endpoints"][0]["secret_configured"] is False

        diagnostic = client.post(
            "/api/v1/sources/src-a/test",
            json={"expected_version": 1},
        )
        assert diagnostic.status_code == 200
        assert diagnostic.json()["completeness"] == "COMPLETE"
        assert client.get("/api/v1/alerts").json() == []
        assert client.get("/api/v1/sources/src-a/audit").json()[-1]["action"] == (
            "TEST_COMPLETE"
        )

        accepted = client.post(
            "/api/v1/sources/src-a/collect",
            headers={"Idempotency-Key": "source-collect-0001"},
            json={"expected_version": 1},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        deadline = time.monotonic() + 2
        state = "PENDING"
        while time.monotonic() < deadline:
            state = client.get(f"/api/v1/jobs/{job_id}").json()["state"]
            if state in {"SUCCEEDED", "FAILED", "CANCELED"}:
                break
            time.sleep(0.01)
        assert state == "SUCCEEDED"

        alerts = client.get("/api/v1/alerts").json()
        incidents = client.get("/api/v1/incidents").json()
        assert [item["upstream_fingerprint"] for item in alerts] == ["target-one"]
        assert len(incidents) == 1
        assert client.get("/api/v1/sources/src-a/monitoring").json() == []
        unavailable_evidence = client.get(
            f"/api/v1/alerts/{alerts[0]['id']}/metric-evidence"
        )
        assert unavailable_evidence.status_code == 200
        assert unavailable_evidence.json()["failures"] == ["SOURCE_UNAVAILABLE"]

        rule = {
            "name": "target by cluster",
            "priority": 10,
            "enabled": True,
            "matchers": [
                {"label": "alertname", "operator": "=", "value": "TargetDown"}
            ],
            "group_by_labels": ["cluster"],
            "source_ids": ["src-a"],
        }
        preview = client.post("/api/v1/aggregation-rules/preview", json=rule)
        assert preview.status_code == 200
        assert preview.json()["selected_alert_count"] == 1
        created_rule = client.post(
            "/api/v1/aggregation-rules",
            headers={"Idempotency-Key": "rule-create-0001"},
            json=rule,
        )
        assert created_rule.status_code == 201
        updated_rule = client.put(
            f"/api/v1/aggregation-rules/{created_rule.json()['id']}",
            json={**rule, "expected_version": 1, "priority": 5},
        )
        assert updated_rule.status_code == 200
        assert updated_rule.json()["version"] == 2

    resources.engine.dispose()


def test_candidate_source_secret_is_encrypted_and_never_echoed(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    payload = {
        "name": "Bearer AM",
        "endpoints": [
            {
                "position": 0,
                "url": "https://bearer-am.invalid",
                "auth_kind": "BEARER",
                "secret": {"action": "REPLACE", "value": "test-only-secret"},
            }
        ],
    }
    with TestClient(resources.app) as client:
        created = client.post(
            "/api/v1/sources",
            headers={"Idempotency-Key": "source-create-0001"},
            json=payload,
        )
        assert created.status_code == 201
        body = created.json()
        assert body["endpoints"][0]["secret_configured"] is True
        assert "test-only-secret" not in created.text
        fetched = client.get(f"/api/v1/sources/{body['id']}")
        assert "test-only-secret" not in fetched.text
        updated = client.put(
            f"/api/v1/sources/{body['id']}",
            json={
                **payload,
                "name": "Bearer AM renamed",
                "expected_version": 1,
                "endpoints": [
                    {
                        "position": 0,
                        "url": "https://bearer-am.invalid",
                        "auth_kind": "BEARER",
                        "secret": {"action": "KEEP"},
                    }
                ],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["endpoints"][0]["secret_configured"] is True
        tested = client.post(
            f"/api/v1/sources/{body['id']}/test",
            json={"expected_version": 2},
        )
        assert tested.status_code == 200

    database_bytes = (tmp_path / "incident-operations.db").read_bytes()
    assert b"test-only-secret" not in database_bytes
    resources.engine.dispose()


async def test_monitoring_adapter_is_one_bounded_get_without_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    endpoint = EndpointSnapshot(
        source_id="src-a",
        source_version=1,
        position=0,
        canonical_url="http://10.0.0.8:9093",
        auth_kind="NONE",
        username="",
        secret="",
    )
    result = await BoundedAlertmanagerReader(
        transport=httpx.MockTransport(handler)
    ).fetch(endpoint)

    assert result.succeeded is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/v2/alerts"


def test_candidate_does_not_replace_current_default_app(tmp_path: Path) -> None:
    from app.main import app as current_app

    resources = _candidate(tmp_path)
    assert resources.app is not current_app
    assert "/api/incidents" in current_app.openapi()["paths"]
    assert "/api/v1/sources" not in current_app.openapi()["paths"]
    resources.engine.dispose()
