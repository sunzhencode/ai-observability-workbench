"""Observability API and default-product isolation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.bootstrap import create_job_platform_app

UTC = timezone.utc


class FakeThanos:
    async def probe(self):
        return True, "OK"

    async def alert_rules(self, alertname: str):
        return ({"type": "alerting", "name": alertname, "query": "sum(up)"},)

    async def query_range(self, query, start, end, step_seconds):
        return {"resultType": "matrix", "result": [{"metric": {}, "values": [[1, "1"]]}]}

    async def query_instant(self, query, at, *, limit=None):
        return {"resultType": "vector", "result": [{"metric": {}, "value": [1, "1"]}]}

    async def series_alerts(self, start, end, *, limit):
        return (
            {
                "__name__": "ALERTS",
                "alertname": "HistoricalTargetDown",
                "cluster": "cluster-history",
                "namespace": "payments",
            },
        )


class FakeGrafana:
    async def search_dashboards(self, query: str, *, limit: int = 50):
        return ({"uid": "kube-main", "title": "Kubernetes", "url": "/d/kube-main"},)

    async def get_dashboard(self, uid: str):
        return {
            "title": "Kubernetes",
            "panels": [
                {
                    "id": 7,
                    "title": "CPU",
                    "datasource": {"type": "prometheus"},
                    "targets": [{"refId": "A", "expr": "sum(up)"}],
                }
            ],
        }


@dataclass(frozen=True)
class ProbeResult:
    ok: bool = True
    safe_error_code: str = "OK"


class FakeModelProbe:
    calls = 0

    async def test(self, *, profile, api_key: str):
        type(self).calls += 1
        assert api_key == "test-api-key"
        assert profile.model_id == "gpt-compatible"
        return ProbeResult()

    async def list_models(self, *, base_url: str, api_key: str):
        return ("gpt-compatible",)


class TimeoutModelProbe(FakeModelProbe):
    async def test(self, *, profile, api_key: str):
        del profile, api_key
        type(self).calls += 1
        return ProbeResult(False, "MODEL_TIMEOUT")


def _resources(tmp_path: Path, *, model_probe=None):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    return create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"candidate-cursor-key-at-least-32-bytes",
        thanos_factory=lambda _url, _secret: FakeThanos(),
        grafana_factory=lambda _url, _secret: FakeGrafana(),
        model_probe=model_probe or FakeModelProbe(),
    )


def _source(client: TestClient) -> str:
    response = client.post(
        "/api/v1/sources",
        headers={"Idempotency-Key": "source-create-observability-0001"},
        json={
            "name": "Primary",
            "endpoints": [{"position": 0, "url": "https://am.invalid"}],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_model_vendor_presets_offer_real_models_before_channel_save(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    with TestClient(resources.app) as client:
        vendors = client.get("/api/v1/model-vendors")

    assert vendors.status_code == 200
    by_id = {item["id"]: item for item in vendors.json()}
    assert by_id["OPENAI"]["recommended_models"] == ["gpt-5.5"]
    assert by_id["DEEPSEEK"]["recommended_models"] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert by_id["DASHSCOPE"]["recommended_models"] == [
        "qwen3.8-max",
        "qwen3.7-plus",
        "qwen3.7-flash",
    ]
    assert by_id["ZHIPU"]["recommended_models"] == ["glm-5.2"]
    assert by_id["MOONSHOT"]["recommended_models"] == [
        "kimi-k2.6",
        "kimi-k2.5",
    ]
    assert by_id["CUSTOM"]["recommended_models"] == []
    assert by_id["OPENAI"]["protocol_profile"] == "RESPONSES"
    assert by_id["OPENAI"]["support_level"] == "REVIEWED"
    assert by_id["DASHSCOPE"]["support_level"] == "COMPATIBLE"
    assert all(
        name not in {"fake-planner", "fake-model"}
        for vendor in by_id.values()
        for name in vendor["recommended_models"]
    )


def test_monitoring_grafana_template_and_model_channel_candidate_flow(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    with TestClient(resources.app) as client:
        source_id = _source(client)
        for kind, url in (("thanos", "http://10.0.0.8:9090"), ("grafana", "http://10.0.0.9:3000")):
            saved = client.put(
                f"/api/v1/sources/{source_id}/monitoring/{kind}",
                json={
                    "base_url": url,
                    "secret": {"action": "REPLACE", "value": "monitor-token"},
                },
            )
            assert saved.status_code == 200
            assert saved.json()["state"] == "DRAFT"
            assert "monitor-token" not in saved.text
            tested = client.post(
                f"/api/v1/sources/{source_id}/monitoring/{kind}/test",
                json={"expected_version": 1},
            )
            assert tested.status_code == 200
            assert tested.json()["state"] == "ACTIVE"

        labels = client.get("/api/v1/aggregation-labels?lookback_hours=168")
        assert labels.status_code == 200, labels.text
        assert labels.json()["history_status"] == "ok"
        assert labels.json()["history_series_count"] == 1
        namespace = next(
            item for item in labels.json()["labels"] if item["name"] == "namespace"
        )
        assert namespace["sources"] == ["history"]

        dashboards = client.get(f"/api/v1/sources/{source_id}/grafana/dashboards")
        assert dashboards.json()[0]["uid"] == "kube-main"
        preview = client.post(
            f"/api/v1/sources/{source_id}/grafana/import/preview",
            json={"dashboard_uid": "kube-main"},
        )
        assert preview.status_code == 200
        assert preview.json()[0]["change_kind"] == "NEW"
        builtins = client.get("/api/v1/metric-templates").json()
        assert len(builtins) == 8
        assert all(item["builtin_key"] and not item["enabled"] for item in builtins)
        candidate = preview.json()[0]
        confirmed = client.post(
            f"/api/v1/sources/{source_id}/grafana/import/confirm",
            json={
                "selections": [
                    {
                        **{key: value for key, value in candidate.items() if key != "change_kind"},
                        "name": "CPU availability",
                        "final_promql": "sum(up)",
                        "priority": 10,
                    }
                ]
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()[0]["origin_kind"] == "GRAFANA"
        assert confirmed.json()[0]["builtin_key"] is None
        repreview = client.post(
            f"/api/v1/sources/{source_id}/grafana/import/preview",
            json={"dashboard_uid": "kube-main"},
        )
        assert repreview.json()[0]["change_kind"] == "UNCHANGED"

        model = client.post(
            "/api/v1/model-channels",
            headers={"Idempotency-Key": "model-channel-create-0001"},
            json={
                "name": "Primary model",
                "kind": "OPENAI_COMPATIBLE",
                "base_url": "https://models.example.invalid/v1",
                "model": "gpt-compatible",
                "api_key": {"action": "REPLACE", "value": "test-api-key"},
            },
        )
        assert model.status_code == 201
        assert "test-api-key" not in model.text
        channel_id = model.json()["id"]
        assert model.json()["state"] == "ACTIVE"
        assert model.json()["enabled"] is True
        listed_models = client.post(
            f"/api/v1/model-channels/{channel_id}/models",
            json={},
        )
        assert listed_models.status_code == 200
        assert listed_models.json() == {"models": ["gpt-compatible"]}
        model_calls_before = FakeModelProbe.calls
        tested_model = client.post(
            f"/api/v1/model-channels/{channel_id}/test",
            headers={"Idempotency-Key": "model-channel-test-0001"},
            json={"expected_revision": 1},
        )
        assert tested_model.status_code == 200
        replayed_test = client.post(
            f"/api/v1/model-channels/{channel_id}/test",
            headers={"Idempotency-Key": "model-channel-test-0001"},
            json={"expected_revision": 1},
        )
        assert replayed_test.json() == tested_model.json()
        assert FakeModelProbe.calls == model_calls_before + 1
        assert tested_model.json()["state"] == "ACTIVE"
        assert tested_model.json()["enabled"] is True

    database = (tmp_path / "incident-operations.db").read_bytes()
    assert b"monitor-token" not in database
    assert b"test-api-key" not in database


def test_model_timeout_is_recorded_without_disabling_and_replayed(tmp_path: Path) -> None:
    TimeoutModelProbe.calls = 0
    probe = TimeoutModelProbe()
    resources = _resources(tmp_path, model_probe=probe)
    with TestClient(resources.app) as client:
        model = client.post(
            "/api/v1/model-channels",
            headers={"Idempotency-Key": "model-timeout-create"},
            json={
                "name": "Timeout model",
                "kind": "OPENAI_COMPATIBLE",
                "base_url": "https://models.example.invalid/v1",
                "model": "gpt-compatible",
                "api_key": {"action": "REPLACE", "value": "test-api-key"},
            },
        ).json()
        path = f"/api/v1/model-channels/{model['id']}/test"
        headers = {"Idempotency-Key": "model-timeout-test"}
        first = client.post(path, headers=headers, json={"expected_revision": 1})
        second = client.post(path, headers=headers, json={"expected_revision": 1})
    assert first.status_code == 200
    assert first.json()["last_test_code"] == "MODEL_TIMEOUT"
    assert first.json()["state"] == "ACTIVE"
    assert first.json()["enabled"] is True
    assert second.json() == first.json()
    assert TimeoutModelProbe.calls == 1
    resources.engine.dispose()


def test_candidate_builtin_templates_preserve_operator_edits(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    with TestClient(resources.app) as client:
        templates = client.get("/api/v1/metric-templates").json()
        target = next(
            item for item in templates if item["builtin_key"] == "pod_container_restarts"
        )
        mutable_fields = (
            "name",
            "promql",
            "enabled",
            "priority",
            "source_ids",
            "description",
            "required_labels",
            "legend_format",
            "unit",
        )
        toggled = client.put(
            f"/api/v1/metric-templates/{target['id']}",
            json={
                **{key: target[key] for key in mutable_fields},
                "enabled": True,
                "expected_version": target["version"],
            },
        )
        assert toggled.status_code == 200
        assert toggled.json()["user_modified"] is False
        edited = client.put(
            f"/api/v1/metric-templates/{target['id']}",
            json={
                **{key: toggled.json()[key] for key in mutable_fields},
                "description": "本地验证过的容器重启增量",
                "expected_version": toggled.json()["version"],
            },
        )
        assert edited.status_code == 200
        assert edited.json()["user_modified"] is True
        rejected = client.request(
            "DELETE",
            f"/api/v1/metric-templates/{target['id']}",
            json={"expected_version": edited.json()["version"]},
        )
        assert rejected.status_code == 409

    resources.engine.dispose()
    restarted = _resources(tmp_path)
    with TestClient(restarted.app) as client:
        target = next(
            item
            for item in client.get("/api/v1/metric-templates").json()
            if item["builtin_key"] == "pod_container_restarts"
        )
        assert target["enabled"] is True
        assert target["description"] == "本地验证过的容器重启增量"
    restarted.engine.dispose()


def test_observability_candidate_does_not_replace_default_app(tmp_path: Path) -> None:
    from app.main import app as current_app

    resources = _resources(tmp_path)
    candidate_paths = resources.app.openapi()["paths"]
    assert "/api/v1/metric-templates" in candidate_paths
    assert "/api/v1/model-channels" in candidate_paths
    assert "/api/v1/suggested-queries" not in candidate_paths
    assert "/api/incidents" in current_app.openapi()["paths"]
    assert "/api/v1/metric-templates" not in current_app.openapi()["paths"]
    resources.engine.dispose()
