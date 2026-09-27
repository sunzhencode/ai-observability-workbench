from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "local-monitoring"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_prometheus_sends_repository_rules_to_local_alertmanager() -> None:
    config = yaml.safe_load((LOCAL / "prometheus.yml").read_text(encoding="utf-8"))
    rules = yaml.safe_load((LOCAL / "alerts.yml").read_text(encoding="utf-8"))

    assert config["global"]["evaluation_interval"] == "5s"
    assert config["rule_files"] == ["/etc/prometheus/alerts.yml"]
    targets = config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]
    assert config["scrape_configs"][0]["static_configs"][0]["targets"] == [
        "localhost:9090"
    ]

    rule_items = rules["groups"][0]["rules"]
    alert_names = {item["alert"] for item in rule_items if "alert" in item}
    assert {"LocalHighMemoryUsage", "LocalHighErrorRate", "Watchdog"} <= alert_names
    assert all(item.get("for", "0s") == "0s" for item in rule_items if "alert" in item)


def test_local_alertmanager_has_no_outbound_integration() -> None:
    raw = (LOCAL / "alertmanager.yml").read_text(encoding="utf-8")
    config = yaml.safe_load(raw)

    assert config["route"]["receiver"] == "local-null"
    assert config["receivers"] == [{"name": "local-null"}]
    for forbidden in (
        "webhook_configs",
        "email_configs",
        "slack_configs",
        "msteams_configs",
        "pagerduty_configs",
        "wechat_configs",
    ):
        assert forbidden not in raw


def test_stack_script_is_pinned_loopback_scoped_and_compose_free() -> None:
    script = _read("start.sh")

    assert 'PROMETHEUS_IMAGE="quay.io/prometheus/prometheus:v3.13.1"' in script
    assert 'ALERTMANAGER_IMAGE="quay.io/prometheus/alertmanager:v0.32.1"' in script
    assert "127.0.0.1:9090:9090" in script
    assert "127.0.0.1:9093:9093" in script
    assert "--network-alias alertmanager" in script
    assert 'STACK_LABEL="com.ai-observability-workbench.local-monitoring=true"' in script
    assert ":/etc/prometheus/prometheus.yml:ro" in script
    assert ":/etc/alertmanager/alertmanager.yml:ro" in script
    assert "docker compose" not in script
    assert "docker-compose" not in script
    assert "docker rm -f" not in script
    assert "docker system prune" not in script


def test_launchers_default_to_isolated_local_monitoring_and_fake_outbound() -> None:
    launcher = _read("start.sh")

    assert "MODE=local" in launcher
    assert "--configured" in launcher
    assert "incident-operations-local.db" in launcher
    assert "INCIDENT_OPERATIONS_NOTIFICATION_FAKE=1" in launcher
    assert "INCIDENT_OPERATIONS_MODEL_FAKE=1" in launcher
    assert "scripts/seed_operations_console.py --local" in launcher
    assert not (ROOT / "start-operations-console.sh").exists()


def test_local_source_seed_uses_only_loopback_monitoring_endpoints() -> None:
    seed = _read("scripts/seed_local_source.py")
    candidate_seed = _read("scripts/seed_operations_console.py")

    assert 'ALERTMANAGER = "http://127.0.0.1:9093"' in seed
    assert 'PROMETHEUS = "http://127.0.0.1:9090"' in seed
    assert '"name": "local-prometheus"' in seed
    assert 'LOCAL_ALERTMANAGER = "http://127.0.0.1:9093"' in candidate_seed
    assert 'LOCAL_PROMETHEUS = "http://127.0.0.1:9090"' in candidate_seed
