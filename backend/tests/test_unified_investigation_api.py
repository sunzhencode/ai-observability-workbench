from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.application.observability import ModelChannelInput, MonitoringConnectionDraft
from app.application.sources import EndpointDraft, SourceDraft
from app.bootstrap import create_job_platform_app
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations


UTC = timezone.utc


class Reader:
    async def query_range(self, *_args):
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"service": "checkout-api"},
                    "values": [[1, "0.08"], [2, "0.08"]],
                }
            ],
        }

    async def metric_names(self, *_args):
        return ("checkout_error_ratio", "checkout_request_rate")

    async def probe(self):
        return True, "OK"

    async def query_instant(self, *_args, **_kwargs):
        return {"resultType": "vector", "result": []}

    async def alert_rules(self, *_args):
        return ()

    async def query_alerts(self, *_args):
        return {"resultType": "matrix", "result": []}

    async def metric_metadata(self, *_args):
        return None

    async def metric_label_names(self, *_args):
        return ()

    async def metric_label_values(self, *_args):
        return ()


def _candidate(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    reader = Reader()
    resources = create_job_platform_app(
        database_path=tmp_path / "candidate.db",
        master_key_path=key,
        cursor_secret=b"unified-investigator-cursor-key-32bytes",
        thanos_factory=lambda _url, _secret: reader,
        model_fake_mode=True,
    )
    resources.sources.create_source(
        SourceDraft(
            id="source-a",
            name="Primary AM",
            endpoints=(EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    source = resources.sources.load_snapshot("source-a", expected_version=1)
    alert = {
        "fingerprint": "unified-member",
        "labels": {
            "alertname": "CheckoutErrorRateHigh",
            "severity": "warning",
            "service": "checkout-api",
            "cluster": "local-cluster",
            "namespace": "local-demo",
            "job": "checkout",
        },
        "annotations": {"summary": "checkout error ratio elevated"},
        "startsAt": "2026-09-04T00:00:00Z",
        "generatorURL": (
            "https://prom.invalid/graph?g0.expr="
            "checkout_error_ratio%7Bservice%3D%22checkout-api%22%7D%3E0.05"
        ),
    }
    resources.sources.apply_collection(
        source,
        merge_endpoint_observations(
            (EndpointObservation(source.endpoints[0], "SUCCESS", (alert,), 2),)
        ),
        observed_at=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )
    resources.observability.save_monitoring_connection(
        MonitoringConnectionDraft("source-a", "THANOS", "http://thanos.internal"),
        expected_version=None,
        now=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )
    resources.observability.record_monitoring_test(
        "source-a",
        "THANOS",
        expected_version=1,
        ok=True,
        safe_error_code="OK",
        now=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )
    channel = resources.observability.create_model_channel(
        ModelChannelInput(
            "Offline model",
            "OPENAI_COMPATIBLE",
            "https://api.openai.com/v1",
            "gpt-test",
            api_key_action="REPLACE",
            api_key_value="offline-test-key",
            provider_id="OPENAI",
        ),
        now=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )
    assert channel.enabled is True
    return resources


def _duplicate_members(resources, total: int) -> None:
    with resources.engine.begin() as connection:
        for index in range(2, total + 1):
            connection.execute(
                text(
                    """
                    INSERT INTO alert (
                      source_id,upstream_fingerprint,alertname,severity,cluster,labels_json,
                      annotations_json,raw_json,origin,evidence_completeness,source_state,
                      missing_since_at,starts_at,last_seen_at,endpoint_positions_json,incident_id
                    ) SELECT source_id,:fingerprint,alertname,severity,cluster,labels_json,
                      annotations_json,raw_json,origin,evidence_completeness,source_state,
                      missing_since_at,starts_at,last_seen_at,endpoint_positions_json,incident_id
                    FROM alert WHERE upstream_fingerprint='unified-member'
                    """
                ),
                {"fingerprint": f"unified-member-{index}"},
            )


def test_v2_snapshot_freezes_all_members_and_scopes_same_metric_per_alert(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    _duplicate_members(resources, 25)

    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        started = client.post(
            f"/api/v1/occurrences/{occurrence_id}/investigator-runs",
            headers={"Idempotency-Key": "unified-coverage-0001"},
        )
        assert started.status_code == 202
        run_id = started.json()["id"]
        assert len(started.json()["alert_evidence"]) == 20
        assert all(
            item["labels"] == {}
            for item in started.json()["alert_evidence"]
        )

    with resources.engine.connect() as connection:
        snapshot = json.loads(
            connection.execute(
                text(
                    "SELECT alert_evidence_json FROM evidence_snapshot_v2 "
                    "WHERE investigation_id=:id"
                ),
                {"id": run_id},
            ).scalar_one()
        )
        scope = json.loads(
            connection.execute(
                text(
                    "SELECT catalog_json FROM investigation_tool_scope_v2 "
                    "WHERE investigation_id=:id"
                ),
                {"id": run_id},
            ).scalar_one()
        )

    assert len(snapshot["all_refs"]) == 25
    assert len(set(snapshot["all_refs"])) == 25
    assert len(snapshot["detailed"]) == 20
    assert snapshot["coverage"] == [
        {
            "alertname": "CheckoutErrorRateHigh",
            "count": 5,
            "severity": "warning",
            "source_state": "FIRING",
        }
    ]
    same_metric = [
        item for item in scope if item["metric_id"] == "checkout_error_ratio"
    ]
    assert len(same_metric) >= 2
    assert len({item["scope_id"] for item in same_metric}) == len(same_metric)
    assert any(item["alert_ref"] for item in same_metric)
    resources.engine.dispose()


def test_new_incident_run_uses_v2_harness_and_never_writes_legacy_tables(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        occurrence_id = client.get("/api/v1/occurrences").json()["items"][0]["id"]
        path = f"/api/v1/occurrences/{occurrence_id}/investigator-runs"
        started = client.post(
            path,
            headers={"Idempotency-Key": "unified-investigation-0001"},
        )
        assert started.status_code == 202
        assert started.json()["schema_version"] == "V2"
        assert started.json()["status"] == "QUEUED"
        assert started.json()["metric_evidence"][0]["status"] == "DATA"
        run_id = started.json()["id"]

        current = started.json()
        deadline = time.monotonic() + 5
        while current["status"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
            time.sleep(0.05)
            current = client.get(f"/api/v1/investigator-runs/{run_id}").json()

        assert current["status"] == "COMPLETED"
        assert current["report"]["summary_zh"].startswith("离线调查")
        assert current["request_count"] == 2
        assert current["tool_call_count"] == 1
        assert current["activities"]
        assert current["feedback"] is None
        assert all(
            "planner" not in item["kind"].lower()
            and "analyst" not in item["kind"].lower()
            for item in current["activities"]
        )
        replay = client.post(
            path,
            headers={"Idempotency-Key": "unified-investigation-0001"},
        )
        assert replay.json()["id"] == run_id
        feedback = client.post(
            f"/api/v1/investigator-runs/{run_id}/feedback",
            json={"rating": "ADOPTED"},
        )
        assert feedback.status_code == 201
        assert feedback.json()["rating"] == "ADOPTED"
        assert client.get(
            f"/api/v1/investigator-runs/{run_id}"
        ).json()["feedback"]["rating"] == "ADOPTED"

    with resources.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM investigation")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM investigation_run_v2")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM investigation_report_v2")).scalar_one() == 1
    resources.engine.dispose()
