"""Keep the final Incident Operations composition root explicit and singular."""

from __future__ import annotations

import asyncio
import json
import runpy
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.adapters.notifications.providers import ScriptedFakeNotificationProvider
from app.domains.investigations.provider_catalog import build_provider_profile
from app.domains.investigations.runtime import ProviderId
from app.operations_console import _trusted_hosts, create_app


ROOT = Path(__file__).resolve().parents[2]
workbench_app = FastAPI()


def test_static_launcher_mode_is_discoverable_without_starting_services() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "start.sh"), "--static", "--help"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "--static" in result.stdout


@pytest.mark.parametrize("listen_host", ["0.0.0.0", "127.0.0.1"])
def test_candidate_seed_connects_through_loopback_for_wildcard_bind(monkeypatch, listen_host) -> None:
    monkeypatch.setenv("INCIDENT_OPERATIONS_HOST", listen_host)
    monkeypatch.setenv("INCIDENT_OPERATIONS_PORT", "18100")
    namespace = runpy.run_path(str(ROOT / "scripts/seed_operations_console.py"))
    assert namespace["BACKEND"] == "http://127.0.0.1:18100"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_default_launcher_owns_the_v1_console_and_configured_database() -> None:
    launcher = _read("start.sh")
    client = _read("operations-console/src/api/client.ts")

    assert "app.operations_console:create_app" in launcher
    assert "--factory" in launcher
    assert "npm --prefix operations-console" in launcher
    assert "$ROOT/backend/data/workbench.db" in launcher
    assert "STATIC=1" in launcher
    assert "/api/v1" in client
    assert '"/api/incidents' not in client
    assert "app.main:app" not in launcher
    assert "npm --prefix frontend" not in launcher


def test_only_one_launcher_remains() -> None:
    assert not (ROOT / "start-operations-console.sh").exists()
    assert not (ROOT / "scripts/local_monitoring_stack.sh").exists()


def test_final_factory_accepts_the_reset_workbench_database(monkeypatch, tmp_path: Path) -> None:
    database = tmp_path / "workbench.db"
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    captured = {}

    class Resources:
        app = workbench_app

    def capture_factory(**kwargs):
        captured.update(kwargs)
        return Resources()

    monkeypatch.setenv("INCIDENT_OPERATIONS_DATABASE_PATH", str(database))
    monkeypatch.setenv("INCIDENT_OPERATIONS_MASTER_KEY_PATH", str(key))
    monkeypatch.setattr("app.operations_console.create_job_platform_app", capture_factory)

    assert create_app() is workbench_app
    assert captured["database_path"] == database.resolve()


def test_candidate_mock_mode_injects_network_free_outbound_adapters(monkeypatch, tmp_path: Path) -> None:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    captured = {}

    class Resources:
        app = workbench_app

    def capture_factory(**kwargs):
        captured.update(kwargs)
        return Resources()

    monkeypatch.setenv("INCIDENT_OPERATIONS_DATABASE_PATH", str(tmp_path / "candidate.db"))
    monkeypatch.setenv("INCIDENT_OPERATIONS_MASTER_KEY_PATH", str(key))
    monkeypatch.setenv("INCIDENT_OPERATIONS_NOTIFICATION_FAKE", "1")
    monkeypatch.setenv("INCIDENT_OPERATIONS_MODEL_FAKE", "1")
    monkeypatch.setattr("app.operations_console.create_job_platform_app", capture_factory)
    create_app()

    provider = captured["notification_provider_factory"]("GENERIC_WEBHOOK")
    assert isinstance(provider, ScriptedFakeNotificationProvider)
    probe = captured["model_probe"]
    result = asyncio.run(
        probe.test(
            profile=build_provider_profile(
                ProviderId.CUSTOM,
                model_id="x",
                custom_base_url="https://model.invalid",
            ),
            api_key="secret",
        )
    )
    assert result.ok is True
    assert result.safe_error_code == "OK"
    assert asyncio.run(
        probe.list_models(base_url="https://model.invalid", api_key="secret")
    ) == ()
    client = captured["model_client_factory"]("OPENAI_COMPATIBLE", object())
    client._visible_delay_seconds = 0
    base_payload = {
        "available_metric_names": ["up"],
        "alerts": [{"alert_ref": "alert-a"}],
        "described_metrics": [],
        "previous_steps": [],
        "catalog_revision": "catalog-1",
    }
    first = asyncio.run(client.complete(messages=(
        {"role": "user", "content": json.dumps(base_payload)},
    )))
    assert first.tool_calls[0].name == "DescribeMetric"
    second_payload = {
        **base_payload,
        "described_metrics": [{"name": "up"}],
        "previous_steps": [{"action": "DESCRIBE_METRIC", "metric_name": "up"}],
    }
    second = asyncio.run(client.complete(messages=(
        {"role": "user", "content": json.dumps(second_payload)},
    )))
    assert second.tool_calls[0].name == "QueryMetric"
    final_payload = {
        **second_payload,
        "previous_steps": [
            *second_payload["previous_steps"],
            {"action": "QUERY_METRIC", "metric_name": "up"},
        ],
    }
    final = asyncio.run(client.complete(messages=(
        {"role": "user", "content": json.dumps(final_payload)},
    )))
    assert final.text == '{"action":"FINISH"}'

    checkout_payload = {
        **base_payload,
        "available_metric_names": [
            "checkout_http_error_ratio",
            "checkout_http_request_rate",
        ],
    }
    checkout = asyncio.run(client.complete(messages=(
        {"role": "user", "content": json.dumps(checkout_payload)},
    )))
    assert checkout.tool_calls[0].arguments == {
        "metric_name": "checkout_http_request_rate"
    }
    assert captured["model_fake_mode"] is True


def test_candidate_local_monitoring_keeps_notifications_fake_and_models_remote(
    monkeypatch, tmp_path: Path
) -> None:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    captured = {}

    class Resources:
        app = workbench_app

    def capture_factory(**kwargs):
        captured.update(kwargs)
        return Resources()

    monkeypatch.setenv("INCIDENT_OPERATIONS_DATABASE_PATH", str(tmp_path / "candidate.db"))
    monkeypatch.setenv("INCIDENT_OPERATIONS_MASTER_KEY_PATH", str(key))
    monkeypatch.setenv("INCIDENT_OPERATIONS_NOTIFICATION_FAKE", "1")
    monkeypatch.delenv("INCIDENT_OPERATIONS_MODEL_FAKE", raising=False)
    monkeypatch.setattr("app.operations_console.create_job_platform_app", capture_factory)
    create_app()

    provider = captured["notification_provider_factory"]("GENERIC_WEBHOOK")
    assert isinstance(provider, ScriptedFakeNotificationProvider)
    assert captured["model_probe"] is None
    assert captured["model_client_factory"] is None
    assert captured["model_fake_mode"] is False


def test_candidate_remote_hosts_are_explicit(monkeypatch) -> None:
    monkeypatch.setenv(
        "INCIDENT_OPERATIONS_TRUSTED_HOSTS", "ops-mac.local,192.168.1.20"
    )
    assert _trusted_hosts() == ("ops-mac.local", "192.168.1.20")
