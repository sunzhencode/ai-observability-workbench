"""Characterization tests for the deliberately retired Suggested Query surface.

The investigation design removes model-authored Suggested Query, not metric evidence in general.
Keep the removal target and the protected neighboring contracts explicit so a
mechanical router/module deletion cannot silently erase the useful F27/F28
paths with it.
"""

from __future__ import annotations


SUGGESTED_QUERY_PATHS = {
    "/api/alerts/{alert_id}/suggested-queries": {"post"},
    "/api/alerts/{alert_id}/suggested-queries/preview": {"get"},
}

PROTECTED_METRIC_PATHS = {
    "/api/alerts/{alert_id}/metric-evidence": {"get"},
    "/api/metric-templates": {"get", "post"},
    "/api/metric-templates/{template_id}": {"patch", "delete"},
    "/api/event-sources/{source_id}/grafana/import/preview": {"post"},
    "/api/event-sources/{source_id}/grafana/import/confirm": {"post"},
}


def _mounted_methods() -> dict[str, set[str]]:
    from app.main import app

    operation_names = {"get", "post", "put", "patch", "delete"}
    return {
        path: set(item).intersection(operation_names)
        for path, item in app.openapi()["paths"].items()
    }


def test_model_authored_suggested_query_routes_are_retired() -> None:
    """No runtime route may let a model author or preview raw PromQL."""
    mounted = _mounted_methods()

    assert all(path not in mounted for path in SUGGESTED_QUERY_PATHS)


def test_suggested_query_retirement_must_preserve_metric_paths() -> None:
    """Deleting Suggested Query must not delete evidence/templates/import."""
    mounted = _mounted_methods()

    assert {
        path: mounted.get(path, set()) for path in PROTECTED_METRIC_PATHS
    } == PROTECTED_METRIC_PATHS
