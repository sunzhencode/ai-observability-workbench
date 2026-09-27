"""Pure label discovery statistics for alert-type configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.models import Alert
from app.services.alert_types import WATCHDOG_ALERTNAME

EXCLUDED_LABELS = {
    "__name__",
    "alertname",
    "alertstate",
    "__datasource_type__",
    "__datasource_uid__",
    "environment",
    "source_id",
}
GLOBAL_EXCLUDED_LABELS = {
    "__name__",
    "alertstate",
    "__datasource_type__",
    "__datasource_uid__",
    "environment",
    "source_id",
}


@dataclass(frozen=True)
class DiscoveredLabel:
    name: str
    coverage: float
    present_count: int
    total_count: int
    distinct_count: int
    sample_values: list[str]
    sources: list[str]


@dataclass(frozen=True)
class DiscoveredAlertType:
    alertname: str
    record_count: int
    labels: list[DiscoveredLabel]


def summarize_global_labels(
    current_alerts: Iterable[Alert],
    history_series: Iterable[dict[str, str]],
) -> list[DiscoveredLabel]:
    """Build one rule-editor label catalog, independent of any alert type."""
    rows: list[tuple[str, dict[str, str]]] = []
    for alert in sorted(current_alerts, key=lambda item: item.fingerprint):
        if alert.alertname == WATCHDOG_ALERTNAME:
            continue
        labels = {
            str(key): str(value)
            for key, value in (alert.labels or {}).items()
            if value is not None
        }
        labels.setdefault("alertname", alert.alertname)
        labels.setdefault("severity", alert.severity)
        labels.setdefault("cluster", alert.cluster)
        rows.append(("current", labels))
    for raw in sorted(
        (dict(item) for item in history_series if isinstance(item, dict)),
        key=lambda item: tuple(sorted((str(k), str(v)) for k, v in item.items())),
    ):
        labels = {str(key): str(value) for key, value in raw.items() if value is not None}
        if str(labels.get("alertname") or "").strip() == WATCHDOG_ALERTNAME:
            continue
        rows.append(("history", labels))

    total = len(rows)
    names = sorted(
        {
            name
            for _, labels in rows
            for name, value in labels.items()
            if name not in GLOBAL_EXCLUDED_LABELS and str(value).strip()
        }
    )
    result: list[DiscoveredLabel] = []
    for name in names:
        present_rows = [
            (source, str(labels[name]))
            for source, labels in rows
            if labels.get(name) is not None and str(labels[name]).strip()
        ]
        values = sorted({value for _, value in present_rows})
        source_set = {source for source, _ in present_rows}
        result.append(
            DiscoveredLabel(
                name=name,
                coverage=len(present_rows) / total if total else 0.0,
                present_count=len(present_rows),
                total_count=total,
                distinct_count=len(values),
                sample_values=values[:10],
                sources=[
                    source for source in ("current", "history") if source in source_set
                ],
            )
        )
    return result


def summarize_alert_labels(
    current_alerts: Iterable[Alert],
    history_series: Iterable[dict[str, str]],
) -> list[DiscoveredAlertType]:
    records: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for alert in sorted(current_alerts, key=lambda item: item.fingerprint):
        if alert.alertname == WATCHDOG_ALERTNAME:
            continue
        labels = {
            str(key): str(value)
            for key, value in (alert.labels or {}).items()
            if value is not None
        }
        records.setdefault(alert.alertname, []).append(("current", labels))
    history_rows = sorted(
        (dict(item) for item in history_series if isinstance(item, dict)),
        key=lambda item: tuple(sorted((str(k), str(v)) for k, v in item.items())),
    )
    for labels in history_rows:
        alertname = str(labels.get("alertname") or "").strip()
        if not alertname or alertname == WATCHDOG_ALERTNAME:
            continue
        records.setdefault(alertname, []).append(
            ("history", {str(key): str(value) for key, value in labels.items()})
        )

    result: list[DiscoveredAlertType] = []
    for alertname in sorted(records):
        rows = records[alertname]
        total = len(rows)
        names = sorted(
            {
                name
                for _, labels in rows
                for name, value in labels.items()
                if name not in EXCLUDED_LABELS and str(value).strip()
            }
        )
        stats: list[DiscoveredLabel] = []
        for name in names:
            present_rows = [
                (source, str(labels[name]))
                for source, labels in rows
                if labels.get(name) is not None and str(labels[name]).strip()
            ]
            values = sorted({value for _, value in present_rows})
            source_set = {source for source, _ in present_rows}
            sources = [source for source in ("current", "history") if source in source_set]
            stats.append(
                DiscoveredLabel(
                    name=name,
                    coverage=len(present_rows) / total if total else 0.0,
                    present_count=len(present_rows),
                    total_count=total,
                    distinct_count=len(values),
                    sample_values=values[:5],
                    sources=sources,
                )
            )
        result.append(
            DiscoveredAlertType(
                alertname=alertname,
                record_count=total,
                labels=stats,
            )
        )
    return result
