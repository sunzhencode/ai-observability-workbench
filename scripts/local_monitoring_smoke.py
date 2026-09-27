#!/usr/bin/env python3
"""Verify the real local Prometheus -> Alertmanager HTTP path."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request


PROMETHEUS = "http://127.0.0.1:9090"
ALERTMANAGER = "http://127.0.0.1:9093"
EXPECTED_ALERTS = {"LocalHighMemoryUsage", "LocalHighErrorRate", "Watchdog"}


def get_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def wait_for_alerts() -> list[dict]:
    for _ in range(40):
        payload = get_json(f"{ALERTMANAGER}/api/v2/alerts")
        assert isinstance(payload, list)
        names = {item.get("labels", {}).get("alertname") for item in payload}
        if EXPECTED_ALERTS <= names:
            return payload
        time.sleep(0.5)
    raise AssertionError("Alertmanager did not receive all local Prometheus alerts")


def check_prometheus() -> dict[str, object]:
    rules = get_json(f"{PROMETHEUS}/api/v1/rules")
    assert isinstance(rules, dict) and rules.get("status") == "success"
    rule_items = [
        item
        for group in rules["data"]["groups"]
        for item in group.get("rules", [])
    ]
    assert rule_items and all(item.get("health") == "ok" for item in rule_items)
    firing = {
        item.get("name")
        for item in rule_items
        if item.get("type") == "alerting" and item.get("state") == "firing"
    }
    assert EXPECTED_ALERTS <= firing

    query = urllib.parse.urlencode({"query": "local_demo_memory_usage_ratio"})
    result = get_json(f"{PROMETHEUS}/api/v1/query?{query}")
    assert isinstance(result, dict) and result.get("status") == "success"
    samples = result["data"]["result"]
    assert samples and samples[0]["value"][1] == "0.92"

    series_query = urllib.parse.urlencode({"match[]": "local_demo_memory_usage_ratio"})
    series = get_json(f"{PROMETHEUS}/api/v1/series?{series_query}")
    assert isinstance(series, dict) and series.get("status") == "success"
    assert series["data"]
    return {"healthy_rules": len(rule_items), "firing_alerts": sorted(firing)}


def check_workbench(base_url: str, *, candidate: bool) -> dict[str, object]:
    source_path = "/api/v1/sources" if candidate else "/api/event-sources"
    incident_path = "/api/v1/incidents?limit=20" if candidate else "/api/incidents?limit=20"
    for _ in range(40):
        sources = get_json(f"{base_url}{source_path}")
        incidents = get_json(f"{base_url}{incident_path}")
        assert isinstance(sources, list) and isinstance(incidents, list)
        local = next((item for item in sources if item.get("name") == "local-prometheus"), None)
        state = None if local is None else local.get("state", local.get("lifecycle_state"))
        local_incidents = [
            item
            for item in incidents
            if item.get("source_name") == "local-prometheus"
            and str(item.get("source_state", "")).upper() == "FIRING"
        ]
        if state == "ENABLED" and len(local_incidents) >= 2:
            return {
                "source_state": state,
                "incident_titles": sorted(item["title"] for item in local_incidents),
            }
        time.sleep(0.5)
    raise AssertionError("workbench did not collect the local Alertmanager alerts")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbench-url")
    parser.add_argument("--candidate", action="store_true")
    args = parser.parse_args()

    summary: dict[str, object] = check_prometheus()
    alerts = wait_for_alerts()
    summary["alertmanager_alerts"] = sorted(
        item["labels"]["alertname"] for item in alerts
    )
    if args.workbench_url:
        summary["workbench"] = check_workbench(
            args.workbench_url.rstrip("/"), candidate=args.candidate
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
