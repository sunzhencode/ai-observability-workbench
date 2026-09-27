#!/usr/bin/env python3
"""Register the bundled mock sources through the public API.

`--mock` used to hand the backend an address in the environment, which
migration v6 then adopted into a disabled source nobody ever enabled -- so the
mock stack ran on a configuration path that no user has. Addresses are
configuration and configuration lives in the registry, so seeding goes through
the same endpoints the 系统设置 page uses.

Idempotent by source name: re-running updates the existing sources instead of
adding duplicates. Local mock addresses only; it never reaches the network.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# start.sh exports the resolved bootstrap values; the defaults match
# backend/app/config.py so the script also works when run by hand.
BACKEND = (
    f"http://{os.environ.get('ALERT_WORKBENCH_HOST', '127.0.0.1')}"
    f":{os.environ.get('ALERT_WORKBENCH_PORT', '8000')}"
)
MOCK = "http://127.0.0.1:9999"

SOURCES = [
    {
        "name": "source-a",
        "endpoints": [
            {"url": f"{MOCK}/source-a/endpoint-1"},
            {"url": f"{MOCK}/source-a/endpoint-2"},
        ],
        "poll_interval_seconds": 10,
        "resolution_grace_seconds": 0,
        "watchdog_enabled": True,
        "watchdog_missing_after_seconds": 60,
        "thanos": {"url": MOCK},
    },
    {
        "name": "source-b",
        "endpoints": [{"url": f"{MOCK}/source-b/endpoint-1"}],
        "poll_interval_seconds": 10,
        "resolution_grace_seconds": 0,
    },
]


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


def wait_for_backend(attempts: int = 40) -> bool:
    for _ in range(attempts):
        try:
            request("GET", "/api/health")
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    return False


def endpoint_payload(endpoint: dict) -> dict:
    return {
        "url": endpoint["url"],
        "enabled": True,
        "auth_type": "NONE",
        "username": "",
        "secret": {"action": "CLEAR", "value": None},
    }


def source_payload(source: dict) -> dict:
    payload = {
        key: value
        for key, value in source.items()
        if key not in {"endpoints", "thanos"}
    }
    payload["endpoints"] = [endpoint_payload(item) for item in source["endpoints"]]
    if "thanos" in source:
        payload["thanos"] = {
            "url": source["thanos"]["url"],
            "auth_type": "NONE",
            "username": "",
            "secret": {"action": "CLEAR", "value": None},
            "timeout_seconds": 15,
        }
    return payload


def main() -> int:
    if not wait_for_backend():
        print("seed: backend never became reachable", file=sys.stderr)
        return 1

    existing = {item["name"]: item for item in request("GET", "/api/event-sources")}
    for source in SOURCES:
        payload = source_payload(source)
        current = existing.get(source["name"])
        try:
            if current is None:
                request("POST", "/api/event-sources", {**payload, "enable": True})
                print(f"seed: registered {source['name']}")
            else:
                request(
                    "PATCH",
                    f"/api/event-sources/{current['id']}",
                    {**payload, "expected_version": current["version"]},
                )
                print(f"seed: updated {source['name']}")
        except urllib.error.HTTPError as exc:
            print(
                f"seed: {source['name']} failed: {exc.code} {exc.read().decode()}",
                file=sys.stderr,
            )
            return 1

    seed_grafana()
    return 0


def seed_grafana() -> None:
    """Point source-a at the mock's Grafana endpoints and open the import gate.

    The gate is a real one — importing is refused until an explicit test has
    succeeded — so the seed has to call the test endpoint rather than write a
    status directly. Doing it the long way is the point: if the gate ever stops
    working, the `--mock` stack stops working with it.

    Only ever the local mock address. This script never reaches the network, and
    a real Grafana address is something only the user can enter.
    """

    sources = {item["name"]: item for item in request("GET", "/api/event-sources")}
    source = sources.get("source-a")
    if source is None:
        return
    try:
        request(
            "PUT",
            f"/api/event-sources/{source['id']}/grafana",
            {"url": MOCK, "secret": {"action": "CLEAR", "value": None}, "timeout_seconds": 15},
        )
        result = request("POST", f"/api/event-sources/{source['id']}/grafana/test")
    except urllib.error.HTTPError as exc:
        print(
            f"seed: grafana for source-a failed: {exc.code} {exc.read().decode()}",
            file=sys.stderr,
        )
        return
    status = "ok" if isinstance(result, dict) and result.get("ok") else "failed"
    print(f"seed: grafana for source-a configured and tested ({status})")


if __name__ == "__main__":
    raise SystemExit(main())
