#!/usr/bin/env python3
"""Idempotently register the disposable local monitoring stack."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


BACKEND = (
    f"http://{os.environ.get('ALERT_WORKBENCH_HOST', '127.0.0.1')}"
    f":{os.environ.get('ALERT_WORKBENCH_PORT', '8000')}"
)
ALERTMANAGER = "http://127.0.0.1:9093"
PROMETHEUS = "http://127.0.0.1:9090"


def request(method: str, path: str, payload: dict | None = None) -> dict | list:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BACKEND}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read() or b"null")


def wait_for_backend() -> bool:
    for _ in range(40):
        try:
            request("GET", "/api/health")
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    return False


def source_payload() -> dict:
    return {
        "name": "local-prometheus",
        "endpoints": [
            {
                "url": ALERTMANAGER,
                "enabled": True,
                "auth_type": "NONE",
                "username": "",
                "secret": {"action": "CLEAR", "value": None},
            }
        ],
        "poll_interval_seconds": 10,
        "resolution_grace_seconds": 0,
        "watchdog_enabled": True,
        "watchdog_missing_after_seconds": 60,
        "thanos": {
            "url": PROMETHEUS,
            "auth_type": "NONE",
            "username": "",
            "secret": {"action": "CLEAR", "value": None},
            "timeout_seconds": 15,
        },
    }


def main() -> int:
    if not wait_for_backend():
        print("seed: backend never became reachable", file=sys.stderr)
        return 1

    existing = {item["name"]: item for item in request("GET", "/api/event-sources")}
    current = existing.get("local-prometheus")
    payload = source_payload()
    try:
        if current is None:
            current = request("POST", "/api/event-sources", {**payload, "enable": True})
            print("seed: registered local-prometheus")
        else:
            current = request(
                "PATCH",
                f"/api/event-sources/{current['id']}",
                {**payload, "expected_version": current["version"]},
            )
            print("seed: updated local-prometheus")
        if isinstance(current, dict) and current.get("lifecycle_state") != "ENABLED":
            request(
                "POST",
                f"/api/event-sources/{current['id']}/enable",
                {"expected_version": current["version"]},
            )
            print("seed: enabled local-prometheus")
    except urllib.error.HTTPError as exc:
        print(
            f"seed: local-prometheus failed: {exc.code} {exc.read().decode()}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
