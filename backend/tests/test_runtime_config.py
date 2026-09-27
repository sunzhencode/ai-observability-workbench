"""Immutable per-job runtime configuration tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import asyncio

import pytest

from app.config import settings
from app.runtime_config import (
    DefaultRuntimeConfig,
    RuntimeConfigProvider,
)
from app.services.normalizer import normalize_alert
from app.services.source_identity import (
    UNMANAGED_SOURCE_ID,
    active_source_id,
)
from app.services.ingest import ingest_alerts
from app.models import Alert
from sqlmodel import select


class StubRuntimeSource:
    """A source whose values can change between reads, unlike the real one."""

    def __init__(self, snapshot):
        self.snapshot_value = snapshot

    def snapshot(self):
        return self.snapshot_value


def test_snapshot_is_immutable_and_switches_only_on_next_read() -> None:
    base = DefaultRuntimeConfig().snapshot()
    source = StubRuntimeSource(replace(base, retention_days=30))
    provider = RuntimeConfigProvider(source)
    old = provider.snapshot()

    source.snapshot_value = replace(base, retention_days=45)
    new = provider.snapshot()

    assert old.retention_days == 30
    assert new.retention_days == 45
    with pytest.raises(FrozenInstanceError):
        old.retention_days = 99  # type: ignore[misc]


def test_snapshot_carries_no_connection_or_secret_fields(monkeypatch) -> None:
    """F21 moved every address and credential into the registry.

    This is a stronger guarantee than the old "secret is not in repr" check:
    the snapshot has nowhere to put a secret at all.
    """
    monkeypatch.setattr(settings, "alertmanager_url", "http://am-secret.test")
    monkeypatch.setattr(settings, "alertmanager_token", "leaky-token-value")
    monkeypatch.setattr(settings, "thanos_url", "http://thanos-secret.test")

    snapshot = DefaultRuntimeConfig().snapshot()

    for field in ("alertmanager", "thanos", "grafana"):
        assert not hasattr(snapshot, field), f"{field} must live in the registry"
    rendered = repr(snapshot)
    assert "leaky-token-value" not in rendered
    assert "am-secret.test" not in rendered
    assert "thanos-secret.test" not in rendered


def test_provider_switch_only_changes_the_next_snapshot() -> None:
    base = DefaultRuntimeConfig().snapshot()
    provider = RuntimeConfigProvider(
        StubRuntimeSource(replace(base, retention_days=7))
    )
    in_flight = provider.snapshot()

    provider.replace_source(StubRuntimeSource(replace(base, retention_days=90)))
    next_job = provider.snapshot()

    assert in_flight.retention_days == 7
    assert next_job.retention_days == 90


@pytest.mark.asyncio
async def test_poll_jobs_never_run_in_parallel(monkeypatch) -> None:
    from app import main

    active = 0
    maximum = 0

    async def fake_poll() -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1

    monkeypatch.setattr(main, "_poll_once_locked", fake_poll)
    await asyncio.gather(main.poll_once(), main.poll_once())

    assert maximum == 1


def test_normalizer_uses_the_captured_snapshot_not_current_settings() -> None:
    """The snapshot-per-job invariant still holds after F21.

    Connections moved to the registry, but a job must still finish with the
    runtime values it captured at its boundary.
    """
    snapshot = replace(DefaultRuntimeConfig().snapshot(), environment="qa")

    normalized = normalize_alert(
        {
            "fingerprint": "fp-1",
            "labels": {"alertname": "TargetDown", "severity": "warning"},
            "annotations": {},
        },
        environment=snapshot.environment,
    )

    assert normalized["environment"] == "qa"
    assert DefaultRuntimeConfig().snapshot().environment == "prod"


def test_ingest_finishes_with_captured_snapshot_after_env_switch(session) -> None:
    captured = replace(DefaultRuntimeConfig().snapshot(), environment="qa")

    ingest_alerts(
        session,
        [
            {
                "fingerprint": "captured-fp",
                "labels": {"alertname": "TargetDown", "severity": "warning"},
                "annotations": {},
            }
        ],
        runtime=captured,
    )

    stored = session.exec(select(Alert)).one()
    assert stored.environment == "qa"
    # Identity comes from the caller's explicit source_id, never from a file.
    assert stored.source_id == active_source_id(captured) == UNMANAGED_SOURCE_ID
