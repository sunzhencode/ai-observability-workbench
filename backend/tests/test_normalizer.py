"""Tests for alert normalization."""

from __future__ import annotations

from app.services.normalizer import NO_CLUSTER, normalize_alert


def test_normalize_basic_fields(sample_alerts):
    raw = sample_alerts[0]
    fields = normalize_alert(raw)
    assert fields["fingerprint"] == "aaa1111"
    assert fields["alertname"] == "KubePodCrashLooping"
    assert fields["severity"] == "critical"
    assert fields["cluster"] == "cluster-a"
    assert fields["environment"] == "prod"
    assert fields["starts_at"] is not None
    assert fields["raw_payload"] == raw


def test_missing_cluster_uses_placeholder(sample_alerts):
    # HighLatency alert has no cluster label.
    high_latency = next(a for a in sample_alerts if a["labels"]["alertname"] == "HighLatency")
    fields = normalize_alert(high_latency)
    assert fields["cluster"] == NO_CLUSTER


def test_unknown_severity_normalized():
    fields = normalize_alert({"fingerprint": "x", "labels": {"alertname": "Foo"}})
    assert fields["severity"] == "unknown"
