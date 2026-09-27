"""Delivery history, Incident status, and manual retry API tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import notification_deliveries
from app.db import get_session
from app.models import (
    Incident,
    NotificationAttempt,
    NotificationDelivery,
    NotificationRoute,
    NotificationRouteTarget,
)
from app.services.source_identity import active_source_id


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(notification_deliveries.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _records(session, *, state: str = "PERMANENT_FAILED") -> tuple[int, int]:
    incident = Incident(
        source_id=active_source_id(),
        group_key="delivery-api",
        title="TargetDown",
        source_state="firing",
    )
    session.add(incident)
    session.flush()
    route = NotificationRoute(
        incident_id=incident.id,
        occurrence_no=1,
        policy_revision_id=1,
        policy_name="test",
        policy_version=1,
        policy_priority=1,
        repeat_interval_seconds=0,
    )
    session.add(route)
    session.flush()
    target = NotificationRouteTarget(
        route_id=route.id,
        channel_id=2,
        routed_channel_revision_id=3,
        channel_name="oncall",
        provider="FEISHU_CUSTOM_BOT",
        channel_version=1,
        mention_mode="NONE",
    )
    session.add(target)
    session.flush()
    delivery = NotificationDelivery(
        event_key="delivery-api-key",
        incident_id=incident.id,
        route_id=route.id,
        route_target_id=target.id,
        event_type="FIRING_OPENED",
        incident_change_version=1,
        state=state,
        payload_snapshot_json={
            "severity": "critical",
            "source_id": "source-for-delivery-api",
            "source_name": "Delivery API Source",
        },
        attempt_count=1,
    )
    session.add(delivery)
    session.flush()
    session.add(
        NotificationAttempt(
            delivery_id=delivery.id,
            attempt_no=1,
            outcome="PERMANENT_FAILURE",
            error_code="HTTP_400",
            error_summary="provider rejected request",
        )
    )
    session.commit()
    return incident.id, delivery.id


def test_delivery_list_detail_and_manual_retry(session) -> None:
    incident_id, delivery_id = _records(session)
    client = _client(session)
    listed = client.get("/api/notification-deliveries")
    assert listed.status_code == 200
    assert listed.json()[0]["channel_name"] == "oncall"
    assert listed.json()[0]["source_id"] == "source-for-delivery-api"
    assert listed.json()[0]["source_name"] == "Delivery API Source"
    detail = client.get(f"/api/notification-deliveries/{delivery_id}")
    assert detail.status_code == 200
    assert detail.json()["attempts"][0]["error_code"] == "HTTP_400"
    assert detail.json()["source_name"] == "Delivery API Source"
    assert "webhook" not in detail.text.lower()

    retried = client.post(f"/api/notification-deliveries/{delivery_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["delivery"]["state"] == "PENDING"
    assert "duplicate" in retried.json()["warning"]

    notification = client.get(f"/api/incidents/{incident_id}/notification")
    assert notification.status_code == 200
    assert notification.json()["route"]["policy_name"] == "test"


def test_successful_delivery_manual_retry_returns_conflict(session) -> None:
    _incident_id, delivery_id = _records(session, state="SUCCEEDED")
    response = _client(session).post(
        f"/api/notification-deliveries/{delivery_id}/retry"
    )
    assert response.status_code == 409
