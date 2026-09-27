"""F20 EventSource REST contract tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.api import event_sources as event_sources_api
from app.config import settings
from app.config_schemas import safe_validation_exception_handler
from app.db import get_session
from app.registry_models import F20Model
from app.services.event_sources import EndpointTestResult


@pytest.fixture
def api_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@dataclass
class AlwaysOkTester:
    async def test(self, endpoint, secret: str) -> EndpointTestResult:
        return EndpointTestResult(True, "OK")


def _client(session) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, safe_validation_exception_handler)
    app.include_router(event_sources_api.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[event_sources_api.get_endpoint_tester] = AlwaysOkTester
    return TestClient(app)


def _payload(*, name: str = "API source", enable: bool = True) -> dict:
    return {
        "name": name,
        "enable": enable,
        "endpoints": [
            {
                "url": "https://api-source.invalid",
                "enabled": True,
                "auth_type": "BEARER",
                "username": "",
                "secret": {"action": "REPLACE", "value": "api-source-secret"},
            }
        ],
        "poll_interval_seconds": 30,
        "resolution_grace_seconds": 60,
        "max_parallel_endpoints": 1,
        "watchdog_enabled": False,
        "watchdog_alertname": "Watchdog",
        "watchdog_identity_label": "cluster",
        "watchdog_missing_after_seconds": 90,
    }


def test_create_list_detail_and_openapi_are_secret_free(
    api_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "master_key", "event-source-api-key")
    client = _client(api_session)
    secret = "api-source-secret"

    created = client.post("/api/event-sources", json=_payload())
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    assert created.json()["status"] == "ENABLED"
    assert secret not in created.text
    assert "ciphertext" not in created.text
    assert "internal_state" not in created.text
    assert "environment" not in created.text

    listed = client.get("/api/event-sources")
    detailed = client.get(f"/api/event-sources/{source_id}")
    audits = client.get(f"/api/event-sources/{source_id}/audit")
    assert listed.status_code == detailed.status_code == audits.status_code == 200
    assert secret not in listed.text + detailed.text + audits.text
    assert audits.json()

    schema = client.get("/openapi.json").text
    assert "secret_envelope" not in schema
    assert "internal_state" not in schema
    assert "environment" not in schema


def test_update_test_disable_archive_and_conflict(api_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "master_key", "event-source-api-key")
    client = _client(api_session)
    source = client.post("/api/event-sources", json=_payload()).json()
    source_id = source["id"]
    update = _payload(name="Updated source")
    update.pop("enable")
    update["expected_version"] = 1
    update["endpoints"][0]["url"] = "https://updated-source.invalid"
    update["endpoints"][0]["secret"] = {"action": "KEEP"}

    saved = client.patch(f"/api/event-sources/{source_id}", json=update)
    assert saved.status_code == 200, saved.text
    # Saving is applying: the new address is in service immediately.
    assert saved.json()["status"] == "ENABLED"
    assert (
        saved.json()["config"]["endpoints"][0]["url"]
        == "https://updated-source.invalid"
    )
    conflict = client.patch(f"/api/event-sources/{source_id}", json=update)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "REVISION_CONFLICT"

    tested = client.post(f"/api/event-sources/{source_id}/test", json={})
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    # The apply action is gone, not merely unused.
    assert client.post(
        f"/api/event-sources/{source_id}/apply", json={"expected_version": 2}
    ).status_code == 404
    disabled = client.post(
        f"/api/event-sources/{source_id}/disable",
        json={"expected_version": 2},
    )
    assert disabled.status_code == 200
    archived = client.post(
        f"/api/event-sources/{source_id}/archive",
        json={"expected_version": 3},
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"


def test_history_address_round_trips_without_echoing_its_token(
    api_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "master_key", "event-source-api-key")
    client = _client(api_session)
    payload = _payload(name="With history")
    payload["thanos"] = {
        "url": "https://thanos.invalid",
        "auth_type": "BEARER",
        "secret": {"action": "REPLACE", "value": "thanos-token-never-echoed"},
        "timeout_seconds": 20,
    }

    created = client.post("/api/event-sources", json=payload)

    assert created.status_code == 201, created.text
    assert "thanos-token-never-echoed" not in created.text
    thanos = created.json()["config"]["thanos"]
    assert thanos["url"] == "https://thanos.invalid"
    assert thanos["secret_configured"] is True
    assert thanos["timeout_seconds"] == 20


def test_validation_errors_never_echo_endpoint_secret(api_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "master_key", "event-source-api-key")
    client = _client(api_session)
    payload = _payload(name="")
    payload["endpoints"][0]["secret"]["value"] = "never-echo-invalid-secret"

    response = client.post("/api/event-sources", json=payload)

    assert response.status_code == 422
    assert "never-echo-invalid-secret" not in response.text
    assert '"input"' not in response.text


def test_credential_free_source_saves_without_a_master_key(
    api_session, monkeypatch
) -> None:
    """No credential, no master key needed.

    The box used to be built before anyone knew whether a secret was involved,
    so an unauthenticated Alertmanager could not be saved at all on a machine
    with no `ALERT_WORKBENCH_MASTER_KEY`.
    """
    monkeypatch.setattr(settings, "master_key", "")
    client = _client(api_session)
    payload = _payload(name="Open Alertmanager")
    payload["endpoints"][0]["auth_type"] = "NONE"
    payload["endpoints"][0]["secret"] = {"action": "CLEAR", "value": None}

    created = client.post("/api/event-sources", json=payload)
    assert created.status_code == 201, created.text
    source = created.json()

    update = {key: value for key, value in payload.items() if key != "enable"}
    edited = client.patch(
        f"/api/event-sources/{source['id']}",
        json={**update, "name": "Renamed", "expected_version": source["version"]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "Renamed"


def test_an_unusable_master_key_file_fails_closed(api_session, monkeypatch) -> None:
    """Fail closed, but say what to fix — a bare SECRET_UNAVAILABLE did not.

    Nobody configures the master key any more, so the only way to reach this
    path is a local key file that cannot be created or read.
    """
    from app import master_key
    from app.crypto import SecretUnavailableError

    monkeypatch.setattr(settings, "master_key", "")
    monkeypatch.setattr(
        master_key,
        "master_key",
        lambda: (_ for _ in ()).throw(SecretUnavailableError("no key file")),
    )
    client = _client(api_session)

    created = client.post("/api/event-sources", json=_payload(name="Token source"))
    assert created.status_code == 503
    assert created.json()["detail"] == "MASTER_KEY_NOT_CONFIGURED"
    assert "api-source-secret" not in created.text
