#!/usr/bin/env python3
"""Idempotently register local or bundled mock sources in Incident Operations."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from html.parser import HTMLParser


_listen_host = os.environ.get("INCIDENT_OPERATIONS_HOST", "127.0.0.1")
# A wildcard selects listening interfaces; local bootstrap uses a concrete host.
_connect_host = "127.0.0.1" if _listen_host == "0.0.0.0" else _listen_host
BACKEND = (
    f"http://{_connect_host}"
    f":{os.environ.get('INCIDENT_OPERATIONS_PORT', '8100')}"
)
MOCK = "http://127.0.0.1:9999"
LOCAL_ALERTMANAGER = "http://127.0.0.1:9093"
LOCAL_PROMETHEUS = "http://127.0.0.1:9090"
csrf_token = ""


class CsrfParser(HTMLParser):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        global csrf_token
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            csrf_token = values.get("content") or ""


def request(method: str, path: str, payload: dict | None = None) -> dict | list:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if method not in {"GET", "HEAD"} and csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    if method == "POST" and path in {
        "/api/v1/sources",
        "/api/v1/aggregation-rules",
        "/api/v1/services",
        "/api/v1/service-mapping-rules",
        "/api/v1/metric-templates",
        "/api/v1/model-channels",
        "/api/v1/notification-channels",
        "/api/v1/notification-policies",
    }:
        headers["Idempotency-Key"] = f"operations-seed-{uuid.uuid4()}"
    req = urllib.request.Request(
        f"{BACKEND}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read() or b"null")


def wait_for_backend() -> bool:
    for _ in range(40):
        try:
            request("GET", "/health/live")
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    return False


def source_payload(name: str, endpoints: list[str], *, watchdog: bool | None = None) -> dict:
    return {
        "name": name,
        "endpoints": [
            {
                "position": position,
                "url": url,
                "enabled": True,
                "auth_kind": "NONE",
                "secret": {"action": "CLEAR", "value": None},
            }
            for position, url in enumerate(endpoints)
        ],
        "poll_interval_seconds": 10,
        "resolution_grace_seconds": 0,
        "watchdog_enabled": name == "source-a" if watchdog is None else watchdog,
    }


def ensure_mock_demo_catalog(source_id: str) -> None:
    services = request("GET", "/api/v1/services")
    assert isinstance(services, list)
    service = next(
        (item for item in services if item.get("slug") == "checkout-api"),
        None,
    )
    if service is None:
        service = request(
            "POST",
            "/api/v1/services",
            {
                "name": "Checkout API",
                "slug": "checkout-api",
                "criticality": "TIER_0",
                "links": [],
            },
        )
    assert isinstance(service, dict)

    rules = request("GET", "/api/v1/service-mapping-rules")
    assert isinstance(rules, list)
    rule = next(
        (item for item in rules if item.get("name") == "Checkout API alerts"),
        None,
    )
    payload = {
        "name": "Checkout API alerts",
        "priority": 10,
        "service_id": service["id"],
        "enabled": True,
        "source_ids": [source_id],
        "matchers": [
            {"label": "service", "operator": "=", "value": "checkout-api"}
        ],
    }
    if rule is None:
        rule = request("POST", "/api/v1/service-mapping-rules", payload)
    else:
        expected = {
            key: rule.get(key)
            for key in ("name", "priority", "service_id", "enabled", "source_ids", "matchers")
        }
        if expected != payload:
            rule = request(
                "PUT",
                f"/api/v1/service-mapping-rules/{rule['id']}",
                {**payload, "expected_version": rule["version"]},
            )
    assert isinstance(rule, dict)
    if rule["has_unpublished_changes"]:
        request(
            "POST",
            f"/api/v1/service-mapping-rules/{rule['id']}/publish",
            {"expected_version": rule["version"]},
        )
    print("seed: prepared Checkout API demo mapping")


def ensure_mock_investigator_model() -> None:
    """Install an encrypted synthetic credential only in the isolated mock DB."""

    channels = request("GET", "/api/v1/model-channels")
    assert isinstance(channels, list)
    current = next(
        (item for item in channels if item.get("name") == "本地验收模型"),
        None,
    )
    payload = {
        "name": "本地验收模型",
        "kind": "OPENAI_COMPATIBLE",
        "provider_id": "OPENAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.5",
        "api_key": {"action": "REPLACE", "value": "synthetic-mock-key"},
    }
    if current is None:
        current = request("POST", "/api/v1/model-channels", payload)
    else:
        matches = all(
            current.get(key) == payload[key]
            for key in ("name", "kind", "provider_id", "base_url", "model")
        ) and current.get("api_key_configured") is True
        if not matches:
            current = request(
                "PUT",
                f"/api/v1/model-channels/{current['id']}/draft",
                {**payload, "expected_revision": current["revision_no"]},
            )
        elif not current.get("enabled"):
            current = request(
                "POST",
                f"/api/v1/model-channels/{current['id']}/enable",
                {},
            )
    assert isinstance(current, dict) and current["enabled"] is True
    print("seed: prepared isolated fake Investigator model")


def ensure_mock_notification_channel() -> None:
    """Create a no-secret draft used only to verify editor round-tripping."""

    channels = request("GET", "/api/v1/notification-channels")
    assert isinstance(channels, list)
    current = next(
        (item for item in channels if item.get("name") == "本地验收 Webhook"),
        None,
    )
    payload = {
        "name": "本地验收 Webhook",
        "provider": "GENERIC_WEBHOOK",
        "config": {
            "url": "https://hooks.example.invalid/workbench-acceptance",
            "timeout_seconds": 12,
        },
        "secrets": {},
    }
    if current is None:
        current = request("POST", "/api/v1/notification-channels", payload)
    else:
        matches = all(
            current.get(key) == payload[key]
            for key in ("name", "provider", "config")
        )
        if not matches:
            current = request(
                "PUT",
                f"/api/v1/notification-channels/{current['id']}/draft",
                {**payload, "expected_revision": current["revision_no"]},
            )
    assert isinstance(current, dict)
    print("seed: prepared no-secret notification editor fixture")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if not wait_for_backend():
        print("seed: Incident Operations backend never became reachable", file=sys.stderr)
        return 1
    with urllib.request.urlopen(f"{BACKEND}/", timeout=10) as response:
        parser = CsrfParser()
        parser.feed(response.read().decode("utf-8"))
    if not csrf_token:
        print("seed: Incident Operations page did not expose a CSRF token", file=sys.stderr)
        return 1
    if not args.local:
        ensure_mock_investigator_model()
        ensure_mock_notification_channel()
    existing = {item["name"]: item for item in request("GET", "/api/v1/sources")}
    fixtures = (
        {"local-prometheus": [LOCAL_ALERTMANAGER]}
        if args.local
        else {
            "source-a": [
                f"{MOCK}/source-a/endpoint-1",
                f"{MOCK}/source-a/endpoint-2",
            ],
            "source-b": [f"{MOCK}/source-b/endpoint-1"],
        }
    )
    for name, endpoints in fixtures.items():
        payload = source_payload(name, endpoints, watchdog=True if args.local else None)
        current = existing.get(name)
        if current is None:
            current = request("POST", "/api/v1/sources", payload)
        else:
            current = request(
                "PUT",
                f"/api/v1/sources/{current['id']}",
                {**payload, "expected_version": current["version"]},
            )
        if current["state"] != "ENABLED":
            request(
                "POST",
                f"/api/v1/sources/{current['id']}/enable",
                {"expected_version": current["version"]},
            )
        if name == "source-a" or args.local:
            connections = request(
                "GET", f"/api/v1/sources/{current['id']}/monitoring"
            )
            assert isinstance(connections, list)
            kinds = ("THANOS",) if args.local else ("THANOS", "GRAFANA")
            for kind in kinds:
                existing_connection = next(
                    (item for item in connections if item.get("kind") == kind),
                    None,
                )
                configured = request(
                    "PUT",
                    f"/api/v1/sources/{current['id']}/monitoring/{kind}",
                    {
                        "base_url": LOCAL_PROMETHEUS if args.local else MOCK,
                        "secret": {"action": "CLEAR", "value": None},
                        "expected_version": (
                            None
                            if existing_connection is None
                            else existing_connection["version"]
                        ),
                    },
                )
                assert isinstance(configured, dict)
                tested = request(
                    "POST",
                    f"/api/v1/sources/{current['id']}/monitoring/{kind}/test",
                    {"expected_version": configured["version"]},
                )
                assert isinstance(tested, dict) and tested["last_test_code"] == "OK"
            if not args.local:
                ensure_mock_demo_catalog(str(current["id"]))
        print(f"seed: registered {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
