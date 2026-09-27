"""Composition root for the Incident Operations Console."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
from typing import Any, Mapping, Sequence

from fastapi import FastAPI

from app.adapters.models.openai_compatible import ModelProbeResult
from app.adapters.notifications.providers import (
    NotificationProviderRegistry,
    ScriptedFakeNotificationProvider,
)
from app.bootstrap import create_job_platform_app
from app.domains.investigations.planner import PlannerReplyV1, PlannerToolCallV1


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set by the Incident Operations launcher")
    return Path(value).expanduser().resolve()


def _trusted_hosts() -> tuple[str, ...]:
    value = os.environ.get("INCIDENT_OPERATIONS_TRUSTED_HOSTS", "").strip()
    return tuple(item.strip() for item in value.split(",") if item.strip())


class _OfflineFakeModelProbe:
    async def test(self, *, profile: Any, api_key: str) -> ModelProbeResult:
        del profile, api_key
        return ModelProbeResult(True, "OK")

    async def list_models(self, *, base_url: str, api_key: str) -> tuple[str, ...]:
        del base_url, api_key
        # Fake implementation names are test mechanics, not provider models.
        # Settings already has reviewed vendor recommendations and must never
        # mislabel this process-local probe as an OpenAI model catalogue.
        return ()


class _OfflinePlannerModelClient:
    """Exercise one useful typed read without any model network access."""

    def __init__(self, *, visible_delay_seconds: float = 1.0) -> None:
        self._visible_delay_seconds = max(0.0, visible_delay_seconds)

    async def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] = (),
        response_format: Mapping[str, Any] | None = None,
    ) -> PlannerReplyV1:
        del response_format
        await asyncio.sleep(self._visible_delay_seconds)
        try:
            payload = json.loads(str(messages[-1]["content"]))
            if not tools and isinstance(payload.get("metric_evidence"), list):
                facts = payload["metric_evidence"]
                if not facts:
                    return PlannerReplyV1(text="{}")
                evidence_ref = str(facts[0].get("evidence_ref") or "")
                return PlannerReplyV1(text=json.dumps({
                    "summary": "指标证据已取得；当前结论只表达证据支持的相关性。",
                    "hypotheses": [{
                        "title": "指标变化与本次告警同时出现",
                        "explanation": "冻结窗口中的指标事实与告警时间范围一致。",
                        "verdict": "SUPPORTED",
                        "supporting_evidence_ids": [evidence_ref],
                        "contradicting_evidence_ids": [],
                        "missing_evidence": [],
                    }],
                    "missing_evidence": ["应用日志与最近变更记录"],
                    "recommended_actions": [{
                        "kind": "NEXT_CHECK",
                        "description": "核对同一时间窗口的应用日志和最近变更。",
                        "risk": "只读检查，不改变事件或外部系统状态。",
                        "evidence_ids": [evidence_ref],
                    }],
                }, ensure_ascii=False))
            names = tuple(str(item) for item in payload["available_metric_names"])
            alerts = tuple(payload["alerts"])
            described = {
                str(item["name"])
                for item in payload["described_metrics"]
                if isinstance(item, dict) and item.get("name")
            }
            previous = tuple(payload["previous_steps"])
        except (KeyError, TypeError, ValueError):
            return PlannerReplyV1(text='{"action":"FINISH"}')
        if not names or not alerts:
            return PlannerReplyV1(text='{"action":"FINISH"}')
        metric = (
            "checkout_http_request_rate"
            if "checkout_http_request_rate" in names
            else "up" if "up" in names else names[0]
        )
        if metric not in described and not any(
            isinstance(item, dict)
            and item.get("action") == "DESCRIBE_METRIC"
            and item.get("metric_name") == metric
            for item in previous
        ):
            return PlannerReplyV1(tool_calls=(PlannerToolCallV1(
                "offline-describe", "DescribeMetric", {"metric_name": metric}
            ),))
        if metric in described and not any(
            isinstance(item, dict)
            and item.get("action") == "QUERY_METRIC"
            and item.get("metric_name") == metric
            for item in previous
        ):
            return PlannerReplyV1(tool_calls=(PlannerToolCallV1(
                "offline-query",
                "QueryMetric",
                {
                    "alert_ref": str(alerts[0]["alert_ref"]),
                    "metric_name": metric,
                    "label_filters": [],
                    "window": "1h",
                    "aggregation": "raw",
                    "group_by": [],
                    "catalog_revision": str(payload["catalog_revision"]),
                },
            ),))
        return PlannerReplyV1(text='{"action":"FINISH"}')


def create_app() -> FastAPI:
    """Create the final application from launcher-selected explicit paths."""
    database = _required_path("INCIDENT_OPERATIONS_DATABASE_PATH")
    master_key = _required_path("INCIDENT_OPERATIONS_MASTER_KEY_PATH")
    if not master_key.is_file():
        raise RuntimeError("existing Workbench master key is required")

    frontend_dist = Path(__file__).resolve().parents[2] / "operations-console/dist"
    notification_fake_mode = (
        os.environ.get("INCIDENT_OPERATIONS_NOTIFICATION_FAKE", "") == "1"
    )
    model_fake_mode = os.environ.get("INCIDENT_OPERATIONS_MODEL_FAKE", "") == "1"
    notification_providers = None
    model_probe = None
    model_client_factory = None
    if notification_fake_mode:
        notification_providers = NotificationProviderRegistry(
            fake=ScriptedFakeNotificationProvider()
        )
    if model_fake_mode:
        model_probe = _OfflineFakeModelProbe()
        model_client_factory = lambda _kind, _target: _OfflinePlannerModelClient()
    return create_job_platform_app(
        database_path=database,
        master_key_path=master_key,
        cursor_secret=secrets.token_bytes(32),
        frontend_dist=frontend_dist if frontend_dist.is_dir() else None,
        notification_provider_factory=notification_providers,
        model_probe=model_probe,
        model_client_factory=model_client_factory,
        model_fake_mode=model_fake_mode,
        notification_fake_mode=notification_fake_mode,
        trusted_hosts=_trusted_hosts(),
    ).app
