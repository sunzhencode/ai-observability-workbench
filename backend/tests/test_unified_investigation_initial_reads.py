from __future__ import annotations

import asyncio

import pytest

from app.application.unified_investigations import _collect_initial_observations
from app.domains.investigations.runtime import MetricObservationV2


def _observation(metric_id: str) -> MetricObservationV2:
    return MetricObservationV2(
        f"evidence-{metric_id}",
        metric_id,
        "DATA",
        {"latest": 1.0},
        ((1.0, "1"),),
    )


@pytest.mark.asyncio
async def test_initial_reads_limit_concurrency_and_keep_results_completed_before_deadline() -> None:
    active = 0
    maximum = 0
    blocker = asyncio.Event()

    async def read(metric_id: str) -> MetricObservationV2:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            if metric_id != "fast":
                await blocker.wait()
            return _observation(metric_id)
        finally:
            active -= 1

    observations, incomplete = await _collect_initial_observations(
        ("fast", "slow-a", "slow-b", "slow-c"),
        read=read,
        concurrency=2,
        timeout_seconds=0.05,
    )

    assert tuple(item.metric_id for item in observations) == ("fast",)
    assert incomplete is True
    assert maximum == 2
    assert active == 0


@pytest.mark.asyncio
async def test_initial_reads_keep_other_successes_when_one_task_raises() -> None:
    async def read(metric_id: str) -> MetricObservationV2:
        if metric_id == "bad":
            raise RuntimeError("upstream detail must not discard good evidence")
        return _observation(metric_id)

    observations, incomplete = await _collect_initial_observations(
        ("bad", "good"),
        read=read,
        concurrency=2,
        timeout_seconds=1,
    )

    assert tuple(item.metric_id for item in observations) == ("good",)
    assert incomplete is True
