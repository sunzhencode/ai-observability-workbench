"""Small bounded Prometheus exposition registry for the platform shell.

The first batch only needs process/readiness and HTTP request signals.  The
registry intentionally accepts bounded labels chosen by code, not arbitrary
request values; later Job and domain metrics can use the same seam.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricRegistry:
    """Thread-safe, dependency-free Prometheus text registry."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._request_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self._duration_sums: dict[tuple[str, str], float] = defaultdict(float)
        self._duration_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._ready = 0
        self._local_alerts: dict[str, int] = {}

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        method_label = method.upper()[:16]
        route_label = route[:256]
        status_label = str(status_code)
        with self._lock:
            self._request_counts[(method_label, route_label, status_label)] += 1
            self._duration_sums[(method_label, route_label)] += max(
                0.0, duration_seconds
            )
            self._duration_counts[(method_label, route_label)] += 1

    def set_ready(self, ready: bool) -> None:
        with self._lock:
            self._ready = 1 if ready else 0

    def set_local_alert(self, code: str, active: bool) -> None:
        """Expose only code-owned, bounded local operational alert labels."""
        if code not in {
            "SCHEDULER_DELAY",
            "JOB_RUNNER_HEARTBEAT_STALE",
            "JOB_BACKLOG_HIGH",
            "JOB_LEASE_EXPIRED",
        }:
            raise ValueError("unknown local operational alert code")
        with self._lock:
            self._local_alerts[code] = 1 if active else 0

    def render(self) -> str:
        with self._lock:
            ready = self._ready
            request_counts = sorted(self._request_counts.items())
            duration_sums = sorted(self._duration_sums.items())
            duration_counts = dict(self._duration_counts)
            local_alerts = sorted(self._local_alerts.items())

        lines = [
            "# HELP incident_operations_platform_ready Whether the platform accepts requests.",
            "# TYPE incident_operations_platform_ready gauge",
            f"incident_operations_platform_ready {ready}",
            "# HELP incident_operations_http_requests_total HTTP requests completed by route.",
            "# TYPE incident_operations_http_requests_total counter",
        ]
        for (method, route, status), count in request_counts:
            lines.append(
                "incident_operations_http_requests_total"
                f'{{method="{_escape_label(method)}",route="{_escape_label(route)}",'
                f'status="{_escape_label(status)}"}} {count}'
            )
        lines.extend(
            [
                "# HELP incident_operations_http_request_duration_seconds HTTP request duration by route.",
                "# TYPE incident_operations_http_request_duration_seconds summary",
            ]
        )
        for (method, route), total in duration_sums:
            labels = (
                f'method="{_escape_label(method)}",route="{_escape_label(route)}"'
            )
            lines.append(
                f"incident_operations_http_request_duration_seconds_sum{{{labels}}} {total:.9f}"
            )
            lines.append(
                "incident_operations_http_request_duration_seconds_count"
                f"{{{labels}}} {duration_counts[(method, route)]}"
            )
        lines.extend(
            [
                "# HELP incident_operations_local_operational_alert Whether a local operational threshold is active.",
                "# TYPE incident_operations_local_operational_alert gauge",
            ]
        )
        for code, active in local_alerts:
            lines.append(
                f'incident_operations_local_operational_alert{{code="{_escape_label(code)}"}} {active}'
            )
        return "\n".join(lines) + "\n"
