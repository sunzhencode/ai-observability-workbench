"""Print and assert the F20 bundled mock source fault matrix."""

from __future__ import annotations

import json

import mock_alertmanager as mock


def _count_alerts(source_id: str, endpoint_id: str, scenario: str) -> int | str:
    if mock.Handler._source_endpoint_should_fail(source_id, endpoint_id, scenario):
        return "FAILED"
    return len(
        mock.build_source_alerts(
            source_id,
            endpoint_id,
            scenario=scenario,
        )
    )


def build_matrix() -> dict[str, object]:
    matrix: dict[str, object] = {
        "complete": {
            source_id: {
                endpoint_id: _count_alerts(source_id, endpoint_id, "baseline")
                for endpoint_id in mock.SOURCE_ENDPOINTS[source_id]
            }
            for source_id in mock.SOURCE_IDS
        },
        "partial": {
            "source-a": {
                endpoint_id: _count_alerts(
                    "source-a",
                    endpoint_id,
                    "partial_fail",
                )
                for endpoint_id in mock.SOURCE_ENDPOINTS["source-a"]
            },
        },
        "failed": {
            "source-b": {
                endpoint_id: _count_alerts("source-b", endpoint_id, "all_fail")
                for endpoint_id in mock.SOURCE_ENDPOINTS["source-b"]
            },
        },
        "recovery": {
            "source-a": [
                alert["labels"]["alertname"]
                for alert in mock.build_source_alerts(
                    "source-a",
                    "endpoint-1",
                    scenario="recovery",
                )
            ]
        },
        "watchdog": {
            "missing": len(
                mock.build_source_alerts(
                    "source-a",
                    "endpoint-1",
                    scenario="watchdog_missing",
                )
            ),
            "ignored": [
                alert["labels"].get("cluster")
                for alert in mock.build_source_alerts(
                    "source-a",
                    "endpoint-1",
                    scenario="watchdog_ignored",
                )
                if alert["labels"]["alertname"] == "Watchdog"
            ],
        },
        "thanos": {
            "baseline_series": len(mock.build_thanos_series()),
            "overlap_series": len(mock.build_thanos_series(scenario="overlap")),
        },
    }
    return matrix


def main() -> int:
    matrix = build_matrix()
    assert (
        matrix["partial"]["source-a"]["endpoint-2"] == "FAILED"  # type: ignore[index]
    )
    assert (
        matrix["failed"]["source-b"]["endpoint-1"] == "FAILED"  # type: ignore[index]
    )
    assert (
        matrix["thanos"]["overlap_series"]  # type: ignore[index]
        > matrix["thanos"]["baseline_series"]  # type: ignore[index]
    )
    print(json.dumps(matrix, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
