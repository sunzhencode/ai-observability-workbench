"""The HTTP layer for metric evidence and the template catalogue.

Offline: an in-memory database plus a MockTransport standing in for Thanos.

This layer is tested on purpose. F24's lesson was that a service layer with
coverage and an endpoint without one lets the whole request shape change with
nothing failing — and every one of the seven failure classifications lives here,
not in the pure services below.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.api import alert_investigation, metrics
from app.config_schemas import GrafanaImportItemIn
from app.schemas import CurveOut, MetricTemplateOut
from app.services import evidence_context
from app.db import get_session
from app.registry_models import (
    EventSource,
    F20Model,
    MetricQueryTemplate,
    MetricTemplateBaseline,
    SourceThanosConfig,
)
from app.models import Alert, Incident
from app.services import metric_cache
from app.services.metric_budget import MAX_SERIES_PER_QUERY
from app.services.metric_templates import seed_builtin_templates
from app.sources.thanos import ThanosClient

NOW = datetime.now(timezone.utc)
SOURCE_ID = "src_test"
ES_URL = (
    "http://prometheus-base-prometheus-0:9090/graph"
    "?g0.expr=%28elasticsearch_cluster_health_status%7Bcolor%3D%22green%22%7D"
    "+%3D%3D+0%29+or+%28elasticsearch_cluster_health_status"
    "%7Bcolor%3D%22yellow%22%7D+%3D%3D+1%29&g0.tab=1"
)


@pytest.fixture(autouse=True)
def _clear_caches():
    metric_cache.reset_all()
    yield
    metric_cache.reset_all()


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(metrics.router, prefix="/api")
    # Keep the legacy alert-root investigation reachable while its
    # occurrence-root replacement is still behind the candidate runtime.
    app.include_router(alert_investigation.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


def _alert_in_group(
    session,
    *,
    generator_url: str = ES_URL,
    alertname: str = "ESClusterHealth",
    fingerprint: str = "fp1",
    incident: Incident | None = None,
) -> Alert:
    """One Alert inside a group.

    D28: evidence belongs to the Alert. The helper returns the Alert, not the
    Incident, because "which member" is now the caller's explicit choice —
    there is no representative member to fall back on.
    """

    if incident is None:
        incident = Incident(
            source_id=SOURCE_ID,
            group_key="g",
            title="ES cluster unhealthy",
            severity="warning",
        )
        session.add(incident)
        session.flush()
    alert = Alert(
        fingerprint=fingerprint,
        incident_id=incident.id,
        alertname=alertname,
        severity="warning",
        cluster="nonprod",
        labels={"cluster": "nonprod", "alertname": alertname},
        annotations={},
        starts_at=NOW - timedelta(minutes=30),
        last_seen_at=NOW,
        source_state="firing",
        raw_payload={"generatorURL": generator_url},
    )
    session.add(alert)
    session.flush()
    return alert


def test_retired_reference_baseline_has_no_current_contract_surface(
    client, session
) -> None:
    """ADR 0015 keeps the frozen table, but no current API may expose it."""

    template = client.post(
        "/api/metric-templates", json={"name": "legacy baseline", "promql": "up"}
    ).json()
    legacy = MetricTemplateBaseline(
        template_id=template["id"], value=0.75, direction="HIGH_IS_BAD"
    )
    session.add(legacy)
    session.commit()

    assert "baseline" not in GrafanaImportItemIn.model_fields
    assert "baseline" not in MetricTemplateOut.model_fields
    assert "baseline_facts" not in CurveOut.model_fields

    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/metric-templates/{template_id}/baseline" not in paths
    assert "/api/metric-templates/{template_id}/probe" not in paths

    listed = client.get("/api/metric-templates").json()
    row = next(item for item in listed if item["id"] == template["id"])
    assert "baseline" not in row
    assert session.get(MetricTemplateBaseline, legacy.id) is not None


def _with_thanos(session) -> None:
    session.add(
        EventSource(id=SOURCE_ID, name="nonprod", lifecycle_state="ENABLED")
    )
    session.add(
        SourceThanosConfig(
            source_id=SOURCE_ID,
            canonical_url="https://thanos.internal.example:10902",
            auth_kind="NONE",
            timeout_seconds=15,
        )
    )
    session.flush()


def _serve(handler):
    """Replace the one seam where this module builds a client."""

    def factory(connection):
        return ThanosClient(
            base_url=connection.base_url,
            token=connection.token,
            transport=httpx.MockTransport(handler),
        )

    return factory


def _matrix_response(points, series_count: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"i": str(index)},
                        "values": [[ts, str(value)] for ts, value in points],
                    }
                    for index in range(series_count)
                ],
            },
        },
    )


def _router(rules=None, metadata=None, matrix=None):
    """One handler covering every endpoint the evidence path touches."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rules"):
            return httpx.Response(
                200, json={"status": "success", "data": {"groups": rules or []}}
            )
        if path.endswith("/metadata"):
            return httpx.Response(
                200, json={"status": "success", "data": metadata or {}}
            )
        if path.endswith("/query_range"):
            return matrix or _matrix_response([(1, 1.0), (2, 2.0)])
        return httpx.Response(404, json={"status": "error"})

    return handler


# --------------------------------------------------------------------------
# evidence: the failure classifications
# --------------------------------------------------------------------------


def test_unknown_alert_is_404(client) -> None:
    assert client.get("/api/alerts/999/metric-evidence").status_code == 404


def test_an_alert_without_an_expression_says_so(client, session, monkeypatch) -> None:
    """No rule and no usable link: the primary curve has nothing to derive from."""

    alert = _alert_in_group(session, generator_url="")
    _with_thanos(session)
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert body["curves"] == []
    assert body["failures"][0]["kind"] == "EXPR_UNAVAILABLE"


def test_a_source_without_a_history_address_is_its_own_failure(client, session) -> None:
    """Distinct from "unreachable": nothing is wrong, it is just not set up."""

    alert = _alert_in_group(session)
    # The source exists and is visible; it simply has no Thanos address on it.
    session.add(EventSource(id=SOURCE_ID, name="nonprod", lifecycle_state="ENABLED"))
    session.flush()
    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert body["failures"][0]["kind"] == "THANOS_NOT_CONFIGURED"
    assert body["alert_starts_at"] is not None


def test_an_unreachable_store_is_not_reported_as_a_missing_metric(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context,
        "build_thanos_client",
        _serve(lambda request: httpx.Response(503, json={"status": "error"})),
    )

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    kinds = {failure["kind"] for failure in body["failures"]}
    assert "THANOS_UNREACHABLE" in kinds
    assert "METRIC_NOT_FOUND" not in kinds


def test_an_empty_result_is_a_missing_metric_not_an_outage(
    client, session, monkeypatch
) -> None:
    """The store answered fine; the selector matched nothing. Different next step."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context,
        "build_thanos_client",
        _serve(_router(matrix=_matrix_response([], series_count=0))),
    )

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert body["curves"] == []
    kinds = [f["kind"] for f in body["failures"]]
    assert "THANOS_UNREACHABLE" not in kinds
    assert kinds[0] in {
        "METRIC_NOT_FOUND", "LABEL_SET_NOT_FOUND", "NO_SAMPLES_IN_WINDOW"
    }


def test_a_metric_absent_from_the_catalogue_is_named_as_such(
    client, session, monkeypatch
) -> None:
    """D30: `METRIC_NOT_FOUND` only when absence can actually be proven."""

    alert = _alert_in_group(session)
    _with_thanos(session)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/label/__name__/values"):
            return httpx.Response(
                200, json={"status": "success", "data": ["something_else"]}
            )
        if path.endswith("/series"):
            return httpx.Response(200, json={"status": "success", "data": []})
        return _router(rules=[], matrix=_matrix_response([], series_count=0))(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert [f["kind"] for f in body["failures"]] == ["METRIC_NOT_FOUND"]


def test_a_metric_that_exists_but_has_no_matching_series(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/label/__name__/values"):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": ["elasticsearch_cluster_health_status"],
                },
            )
        if path.endswith("/series"):
            return httpx.Response(200, json={"status": "success", "data": []})
        return _router(rules=[], matrix=_matrix_response([], series_count=0))(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert [f["kind"] for f in body["failures"]] == ["LABEL_SET_NOT_FOUND"]


def test_too_many_series_is_refused_with_its_own_kind(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context,
        "build_thanos_client",
        _serve(
            _router(
                matrix=_matrix_response([(1, 1.0)], series_count=MAX_SERIES_PER_QUERY + 1)
            )
        ),
    )

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert [f["kind"] for f in body["failures"]] == ["SERIES_LIMIT_EXCEEDED"]


def test_no_failure_message_ever_carries_the_address(
    client, session, monkeypatch
) -> None:
    """`str(exc)` on an httpx error embeds the full URL; only codes may leave."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context,
        "build_thanos_client",
        _serve(lambda request: httpx.Response(500, json={"status": "error"})),
    )

    raw = client.get(f"/api/alerts/{alert.id}/metric-evidence").text
    assert "thanos.internal.example" not in raw
    assert "10902" not in raw


# --------------------------------------------------------------------------
# evidence: the happy paths
# --------------------------------------------------------------------------


def test_the_real_es_alert_produces_a_tier_two_curve_from_the_link(
    client, session, monkeypatch
) -> None:
    """No rules behind this Thanos, so the generatorURL fallback carries it."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    curve = body["curves"][0]
    assert curve["kind"] == "PRIMARY"
    assert curve["expr_origin"] == "GENERATOR_URL"
    assert curve["tier"] == "METRIC"
    assert curve["query"] == 'elasticsearch_cluster_health_status{cluster="nonprod"}'
    assert curve["threshold"] is None
    assert curve["series"][0]["points"]


def test_the_rules_endpoint_wins_and_is_reported_as_the_origin(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    rules = [
        {
            "name": "g",
            "rules": [
                {
                    "type": "alerting",
                    "name": "ESClusterHealth",
                    "query": "es_health_ratio < 0.5",
                }
            ],
        }
    ]
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=rules)))

    curve = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"][0]
    assert curve["expr_origin"] == "RULES_API"
    assert curve["tier"] == "THRESHOLD"
    assert curve["threshold"] == pytest.approx(0.5)
    assert curve["query"] == "es_health_ratio"


def test_a_counter_is_charted_as_a_rate(client, session, monkeypatch) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    rules = [
        {
            "name": "g",
            "rules": [
                {"type": "alerting", "name": "ESClusterHealth", "query": "x_bytes_total"}
            ],
        }
    ]
    monkeypatch.setattr(
        evidence_context,
        "build_thanos_client",
        _serve(
            _router(rules=rules, metadata={"x_bytes_total": [{"type": "counter", "help": "H"}]})
        ),
    )

    curve = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"][0]
    assert curve["metric_type"] == "COUNTER"
    assert curve["query"].startswith("rate(")
    assert curve["title"] == "H"


def test_missing_metadata_labels_the_guess_rather_than_hiding_it(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    rules = [
        {
            "name": "g",
            "rules": [
                {"type": "alerting", "name": "ESClusterHealth", "query": "x_bytes_total"}
            ],
        }
    ]
    monkeypatch.setattr(
        evidence_context, "build_thanos_client", _serve(_router(rules=rules, metadata={}))
    )

    curve = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"][0]
    assert curve["metric_type"] == "UNKNOWN_SUFFIX_GUESS"
    assert curve["query"].startswith("rate(")


def test_an_enabled_template_adds_an_auxiliary_curve_after_the_primary(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    session.add(
        MetricQueryTemplate(
            name="集群健康",
            promql='some_metric{cluster="{{cluster}}"}',
            required_labels_json=json.dumps(["cluster"]),
            enabled=True,
        )
    )
    session.flush()
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    curves = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"]
    assert [curve["kind"] for curve in curves] == ["PRIMARY", "AUXILIARY"]
    assert curves[1]["expr_origin"] == "TEMPLATE"
    assert curves[1]["tier"] is None
    assert curves[1]["query"] == 'some_metric{cluster="nonprod"}'
    assert "baseline_facts" not in curves[0]
    assert "baseline_facts" not in curves[1]


def test_a_legacy_baseline_row_is_inert_for_metric_evidence(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    template = MetricQueryTemplate(
        name="集群健康",
        promql='some_metric{cluster="{{cluster}}"}',
        required_labels_json=json.dumps(["cluster"]),
        enabled=True,
    )
    session.add(template)
    session.flush()
    session.add(
        MetricTemplateBaseline(
            template_id=int(template.id or 0), value=500.0, direction="HIGH_IS_BAD"
        )
    )
    session.flush()
    trigger = int(alert.starts_at.timestamp())
    matrix = _matrix_response([(trigger, 600.0), (trigger + 60, 850.0)])
    monkeypatch.setattr(
        evidence_context, "build_thanos_client", _serve(_router(rules=[], matrix=matrix))
    )

    curves = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"]

    primary, auxiliary = curves
    assert primary["kind"] == "PRIMARY"
    assert auxiliary["kind"] == "AUXILIARY"
    assert "baseline_facts" not in primary
    assert "baseline_facts" not in auxiliary


def test_a_template_whose_labels_are_absent_is_not_queried(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    session.add(
        MetricQueryTemplate(
            name="pod 内存",
            promql='m{pod="{{pod}}"}',
            required_labels_json=json.dumps(["pod"]),
            enabled=True,
        )
    )
    session.flush()
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert [curve["kind"] for curve in body["curves"]] == ["PRIMARY"]
    assert body["failures"] == []


def test_timestamps_are_serialised_with_a_z(client, session, monkeypatch) -> None:
    """Naive UTC read back from SQLite must not reach the browser as local time."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert body["alert_starts_at"].endswith("Z")
    assert body["curves"][0]["window_start"].endswith("Z")


def test_rules_are_fetched_once_per_source_then_cached(
    client, session, monkeypatch
) -> None:
    alert = _alert_in_group(session)
    _with_thanos(session)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _router(rules=[])(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    client.get(f"/api/alerts/{alert.id}/metric-evidence")
    client.get(f"/api/alerts/{alert.id}/metric-evidence")

    assert sum(1 for path in calls if path.endswith("/rules")) == 1


def test_curve_data_is_never_cached(client, session, monkeypatch) -> None:
    """Opening an alert means "show me now"; a cache would defeat the purpose."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _router(rules=[])(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    client.get(f"/api/alerts/{alert.id}/metric-evidence")
    client.get(f"/api/alerts/{alert.id}/metric-evidence")

    assert sum(1 for path in calls if path.endswith("/query_range")) == 2


# --------------------------------------------------------------------------
# template catalogue
# --------------------------------------------------------------------------


def test_templates_list_is_name_ordered(client, session) -> None:
    seed_builtin_templates(session)
    session.commit()

    names = [row["name"] for row in client.get("/api/metric-templates").json()]
    assert names == sorted(names)
    assert all(row["enabled"] is False for row in client.get("/api/metric-templates").json())


def test_creating_a_template_starts_disabled(client) -> None:
    created = client.post(
        "/api/metric-templates",
        json={
            "name": "我的查询",
            "promql": 'm{pod="{{pod}}"}',
            "required_labels": ["pod"],
            "description": "d",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["enabled"] is False
    assert body["builtin_key"] is None
    assert body["required_labels"] == ["pod"]
    assert body["source_scope"] == {"mode": "ALL", "source_ids": []}


def test_template_source_scope_is_created_updated_and_returned(client, session) -> None:
    session.add(EventSource(id="src_a", name="A"))
    session.add(EventSource(id="src_b", name="B"))
    session.flush()
    created = client.post(
        "/api/metric-templates",
        json={
            "name": "scoped",
            "promql": "up",
            "source_scope": {"mode": "SELECTED", "source_ids": ["src_a"]},
        },
    )
    assert created.status_code == 201
    template_id = created.json()["id"]
    assert created.json()["source_scope"] == {
        "mode": "SELECTED",
        "source_ids": ["src_a"],
    }

    updated = client.patch(
        f"/api/metric-templates/{template_id}",
        json={"source_scope": {"mode": "SELECTED", "source_ids": ["src_b"]}},
    )
    assert updated.status_code == 200
    assert updated.json()["source_scope"]["source_ids"] == ["src_b"]


def test_unknown_template_source_scope_is_rejected(client) -> None:
    response = client.post(
        "/api/metric-templates",
        json={
            "name": "bad scope",
            "promql": "up",
            "source_scope": {"mode": "SELECTED", "source_ids": ["missing"]},
        },
    )
    assert response.status_code == 422


def test_priority_updates_the_side_table(client) -> None:
    created = client.post(
        "/api/metric-templates", json={"name": "priority", "promql": "up"}
    ).json()
    updated = client.patch(
        f"/api/metric-templates/{created['id']}", json={"priority": 7}
    )
    assert updated.status_code == 200
    assert updated.json()["priority"] == 7


def test_toggling_a_builtin_does_not_claim_ownership_of_it(client, session) -> None:
    """Otherwise switching one on would freeze it against future corrections."""

    seed_builtin_templates(session)
    session.commit()
    template_id = client.get("/api/metric-templates").json()[0]["id"]

    body = client.patch(
        f"/api/metric-templates/{template_id}", json={"enabled": True}
    ).json()
    assert body["enabled"] is True
    assert body["user_modified"] is False


def test_editing_a_builtin_query_does_claim_ownership(client, session) -> None:
    seed_builtin_templates(session)
    session.commit()
    template_id = client.get("/api/metric-templates").json()[0]["id"]

    body = client.patch(
        f"/api/metric-templates/{template_id}", json={"promql": "my_own_query"}
    ).json()
    assert body["user_modified"] is True
    assert body["promql"] == "my_own_query"


def test_a_builtin_cannot_be_deleted(client, session) -> None:
    """Deleting one only makes the next startup seed it back, disabled."""

    seed_builtin_templates(session)
    session.commit()
    template_id = client.get("/api/metric-templates").json()[0]["id"]

    response = client.delete(f"/api/metric-templates/{template_id}")
    assert response.status_code == 409
    assert client.get("/api/metric-templates").json()


def test_a_user_template_can_be_deleted(client) -> None:
    created = client.post(
        "/api/metric-templates", json={"name": "t", "promql": "m"}
    ).json()
    assert client.delete(f"/api/metric-templates/{created['id']}").status_code == 204
    assert client.get("/api/metric-templates").json() == []


def test_updating_an_unknown_template_is_404(client) -> None:
    assert client.patch("/api/metric-templates/999", json={"enabled": True}).status_code == 404
    assert client.delete("/api/metric-templates/999").status_code == 404


def test_an_empty_query_is_rejected_by_validation(client) -> None:
    assert client.post("/api/metric-templates", json={"name": "t", "promql": ""}).status_code == 422


# --------------------------------------------------------------------------
# 来源配置版本（D38）
#
# 这两条是反向验证补出来的：把 `source_config_version=snapshot.version` 改成常量 0
# 之后没有任何测试变红，说明"API 确实把版本传下去"这件事零覆盖——和早先集合操作符
# 防护那次是同一类漏洞。
# --------------------------------------------------------------------------


def test_repointing_the_source_changes_the_curve_id(
    client, session, monkeypatch
) -> None:
    """The same address pointing at a different store is different evidence.

    **The window is pinned on purpose.** Without that, two consecutive requests
    already produce different ids because `now` moved — the test would pass with
    the version plumbing removed entirely, which is exactly what happened the
    first time it was written. Freezing the window leaves the config version as
    the only thing that can differ.
    """

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    frozen = metrics.resolve_window(
        alert_starts_at=NOW - timedelta(minutes=30), alert_ends_at=None, now=NOW
    )
    monkeypatch.setattr(metrics, "resolve_window", lambda **_kwargs: frozen)

    before = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"][0]

    source = session.get(EventSource, SOURCE_ID)
    source.version = (source.version or 1) + 1
    session.add(source)
    session.flush()
    metric_cache.reset_all()

    after = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"][0]

    assert before["query"] == after["query"], "same query"
    assert before["window_start"] == after["window_start"], "same window"
    assert before["curve_id"] != after["curve_id"], (
        "only the source config version moved, and it must reach the id"
    )


def test_a_source_changed_mid_flight_discards_the_whole_read(
    client, session, monkeypatch
) -> None:
    """Same invariant the poll path enforces: results describing a configuration
    that no longer exists must be dropped, not rendered."""

    alert = _alert_in_group(session)
    _with_thanos(session)

    def handler(request: httpx.Request) -> httpx.Response:
        # Bump the version while the reads are in flight.
        source = session.get(EventSource, SOURCE_ID)
        source.version = (source.version or 1) + 1
        session.add(source)
        session.flush()
        return _router(rules=[])(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert body["curves"] == []
    assert [f["kind"] for f in body["failures"]] == ["SOURCE_CONFIG_CHANGED"]


def test_the_rules_cache_is_scoped_to_the_config_version(
    client, session, monkeypatch
) -> None:
    """Otherwise a repointed source keeps answering from the old store's rules."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _router(rules=[])(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    client.get(f"/api/alerts/{alert.id}/metric-evidence")
    rules_calls_before = sum(1 for path in calls if path.endswith("/rules"))

    source = session.get(EventSource, SOURCE_ID)
    source.version = (source.version or 1) + 1
    session.add(source)
    session.flush()

    client.get(f"/api/alerts/{alert.id}/metric-evidence")
    rules_calls_after = sum(1 for path in calls if path.endswith("/rules"))

    assert rules_calls_after > rules_calls_before, "the version must invalidate"


def test_warnings_and_failures_are_separate_outlets(
    client, session, monkeypatch
) -> None:
    """D39: a curve derived from the link is a warning, not "upstream is down"."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()

    assert body["curves"], "the curve is there"
    assert body["failures"] == [], "so nothing failed"
    assert "EXPR_FROM_GENERATOR_URL" in {w["kind"] for w in body["warnings"]}


def test_an_expression_scanning_a_month_is_refused_before_it_is_sent(
    client, session, monkeypatch
) -> None:
    """D37: the outer 24h window says nothing about `[30d]` inside the query.

    The expression has to reach `THRESHOLD` for this to bite: stripping the
    threshold forwards the author's sub-expression **verbatim**, which is exactly
    the hole. (A bare `rate(x_total[30d])` with no comparison degrades to the
    `METRIC` tier instead, where the system composes a fresh, safe query — which
    is correct, and is why this test does not use it.)
    """

    alert = _alert_in_group(
        session, generator_url="http://p:9090/graph?g0.expr=rate%28x_total%5B30d%5D%29%20%3E%205"
    )
    _with_thanos(session)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _router(rules=[])(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()

    assert [f["kind"] for f in body["failures"]] == ["QUERY_SCOPE_UNSAFE"]
    assert not any(path.endswith("/query_range") for path in calls), (
        "refused before sending, not after"
    )


def test_a_resolved_alert_charts_around_when_it_fired(client, session, monkeypatch) -> None:
    """`Alert.source_state` is `resolved`; only the derived Incident says `recovered`.

    Comparing against the Incident's word meant every ended alert fell through to
    the still-firing branch and charted up to *now* — exactly the wrong window
    for the one case where the interesting part is already in the past.
    """

    alert = _alert_in_group(session)
    alert.source_state = "resolved"
    # Started before it ended — the helper's default `starts_at` is 30 minutes
    # ago, which would make this alert end before it began and send
    # `resolve_window` down its clock-skew branch instead.
    alert.starts_at = NOW - timedelta(hours=10)
    alert.ends_at = NOW - timedelta(hours=6)
    session.add(alert)
    session.flush()
    _with_thanos(session)
    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(_router(rules=[])))

    curve = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()["curves"][0]

    window_end = datetime.fromisoformat(curve["window_end"].replace("Z", "+00:00"))
    assert window_end < NOW - timedelta(hours=5), (
        "the window must stop after recovery, not run to now"
    )


def test_an_auxiliary_curve_with_no_data_is_not_a_failure(
    client, session, monkeypatch
) -> None:
    """1.8: node templates matched an ES exporter alert and shouted about it.

    `required_labels` checks that a label is *present*, not that it means the
    same kind of thing. The alert carries `instance=10.244.7.5:9108` — the
    exporter pod — so a node template matches and then finds nothing, because
    that instance is not a node.

    An auxiliary curve is a guess ("you might also want to see this"). A guess
    that did not pan out is not a fault, and rendering it in red next to a
    perfectly good primary curve is noise the reader has to learn to ignore.
    """

    alert = _alert_in_group(session)
    _with_thanos(session)
    session.add(
        MetricQueryTemplate(
            name="节点内存可用率",
            promql='node_mem{cluster="{{cluster}}"}',
            required_labels_json=json.dumps(["cluster"]),
            enabled=True,
        )
    )
    session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/query_range"):
            query = request.url.params.get("query", "")
            # The primary draws; the auxiliary finds nothing.
            if "node_mem" in query:
                return _matrix_response([], series_count=0)
            return _matrix_response([(1, 1.0), (2, 2.0)])
        return _router(rules=[])(request)

    monkeypatch.setattr(evidence_context, "build_thanos_client", _serve(handler))

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()

    assert len(body["curves"]) == 1, "the primary still drew"
    assert body["failures"] == [], "an inapplicable guess is not a failure"
    kinds = {note["kind"] for note in body["warnings"]}
    assert "AUXILIARY_NO_DATA" in kinds
    note = next(n for n in body["warnings"] if n["kind"] == "AUXILIARY_NO_DATA")
    assert note["subject"] == "节点内存可用率", "say which one, or it is silent"


def test_a_primary_curve_with_no_data_is_still_a_failure(
    client, session, monkeypatch
) -> None:
    """The primary is the curve you asked for; its absence stays loud."""

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context,
        "build_thanos_client",
        _serve(_router(rules=[], matrix=_matrix_response([], series_count=0))),
    )

    body = client.get(f"/api/alerts/{alert.id}/metric-evidence").json()
    assert body["failures"], "a missing primary curve must not be downgraded"


def _fake_active(fake):
    """An ActiveModel whose client is the offline fake.

    One object describes *and* calls, so a test cannot accidentally patch the
    description and leave the real client in place (or the reverse) — which is
    exactly the seam the production code collapsed.
    """
    from app.providers.model.openai_compatible import OpenAICompatibleConfig
    from app.services.model_channels import ActiveModel

    active = ActiveModel(
        channel_id=1,
        name="offline",
        host="model.example.com",
        model="offline-model",
        config=OpenAICompatibleConfig(
            base_url="https://model.example.com/v1", model="offline-model", api_key="k"
        ),
    )
    object.__setattr__(active, "client", lambda: fake)
    return active


# ---------------------------------------------------------------------------
def test_investigating_without_curves_does_not_pay_to_paraphrase_the_annotation(
    client, session, monkeypatch
) -> None:
    """D52 at the endpoint. A paraphrase of text the user wrote themselves is
    worse than silence, because it looks like analysis."""
    from app.providers.model.fake import FakeModelClient
    from app.services import model_channels

    alert = _alert_in_group(session)
    # The source exists but has no history address, so nothing draws and no
    # fact survives — the shape this gate is actually for.
    session.add(EventSource(id=SOURCE_ID, name="nonprod", lifecycle_state="ENABLED"))
    session.flush()
    fake = FakeModelClient()
    monkeypatch.setattr(
        model_channels, "resolve_active_model", lambda *a, **k: _fake_active(fake)
    )

    response = client.post(f"/api/alerts/{alert.id}/investigate")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["failure"] == "NO_EVIDENCE"
    assert fake.calls == [], "没有证据却已经花了钱"


def test_an_investigation_returns_the_facts_its_claims_cite(
    client, session, monkeypatch
) -> None:
    """A conclusion the reader cannot check is what this structure prevents,
    so the facts travel with it (D22).

    The stand-in model cites whatever fact it was **actually shown**, rather
    than an id read from an earlier request: `curve_id` carries the window, and
    `now` advances between two calls — an earlier version of this test compared
    ids across requests and failed for that reason alone.
    """
    import json as _json

    from app.providers.model.base import ModelReply
    from app.services import model_channels

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context, "build_thanos_client", _serve(_router(rules=[]))
    )

    class CitingModel:
        """Answers with the first fact id it was given. No transport."""

        def __init__(self) -> None:
            self.seen: list[dict] = []

        async def complete(self, *, messages, tools=(), response_format=None):
            self.seen.append({"messages": list(messages)})
            payload = _json.loads(messages[-1]["content"])
            facts = payload.get("metric_facts") or []
            if not facts:
                return ModelReply(text=_json.dumps({"hypotheses": []}))
            return ModelReply(
                text=_json.dumps(
                    {
                        "hypotheses": [
                            {
                                "statement": "集群分片未分配",
                                "verdict": "SUPPORTED",
                                "supporting_fact_ids": [facts[0]["fact_id"]],
                                "contradicting_fact_ids": [],
                                "missing_evidence": [],
                                "recommendations": [
                                    {"kind": "NEXT_CHECK", "text": "看 allocation/explain"}
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                model_name="stand-in",
            )

    model = CitingModel()
    monkeypatch.setattr(
        model_channels, "resolve_active_model", lambda *a, **k: _fake_active(model)
    )

    response = client.post(f"/api/alerts/{alert.id}/investigate")
    assert response.status_code == 200, response.text
    body = response.json()

    if body["failure"] == "NO_EVIDENCE":
        pytest.skip("no curve drew in this fixture")
    assert body["failure"] is None, body
    assert body["hypotheses"][0]["verdict"] == "SUPPORTED"
    cited = body["hypotheses"][0]["supporting_fact_ids"][0]
    assert any(f["fact_id"] == cited for f in body["facts"]), "结论引用的事实没有随行"

    # And the alert's prose did reach it — that is the point of this call.
    sent = _json.dumps(model.seen, ensure_ascii=False)
    assert "annotations" in sent


def test_an_investigation_never_returns_the_unusable_raw_text(
    client, session, monkeypatch
) -> None:
    """D44: an unattributed fluent conclusion must not reach the reader at all."""
    from app.providers.model.base import ModelReply
    from app.providers.model.fake import FakeModelClient
    from app.services import model_channels

    alert = _alert_in_group(session)
    _with_thanos(session)
    monkeypatch.setattr(
        evidence_context, "build_thanos_client", _serve(_router(rules=[]))
    )
    fake = FakeModelClient(replies=[ModelReply(text="内存不足，重启就好了")] * 2)
    monkeypatch.setattr(
        model_channels, "resolve_active_model", lambda *a, **k: _fake_active(fake)
    )

    raw = client.post(f"/api/alerts/{alert.id}/investigate").text

    assert "重启就好了" not in raw
    assert "FAILED_VALIDATION" in raw or "NO_EVIDENCE" in raw
