"""The HTTP layer for the Grafana address, dashboard listing, preview and confirm.

Tested at this level on purpose. F24's lesson was that a service layer with
coverage and an endpoint without one lets the whole request shape change with
nothing failing — and the two properties that matter most here, "preview never
writes" and "no import before an explicit successful test", are both properties
of the endpoints rather than of anything below them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.api import event_sources as event_sources_api
from app.services import event_sources as event_sources_service
from app.api import grafana_import as grafana_import_api
from app.api import metrics as metrics_api
from app.db import get_session
from app.registry_models import (
    EventSource,
    F20Model,
    MetricQueryTemplate,
    MetricTemplateBaseline,
    MetricTemplateOrigin,
    MetricTemplateSourceScope,
    SourceGrafanaConfig,
    SourceThanosConfig,
)
from app.sources.grafana_dashboards import GrafanaDashboardClient
from app.sources.thanos import ThanosClient

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_ID = "src_test"


@pytest.fixture
def dashboard() -> dict:
    with open(FIXTURES / "grafana_dashboard_sample.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        # Built through the service rather than as a bare row: the source has to
        # carry a configuration, which is what `config.grafana` hangs off.
        with mock.patch.object(
            event_sources_service, "new_event_source_id", return_value=SOURCE_ID
        ):
            event_sources_service.create_event_source(
                session=active,
                candidate={
                    "name": "source-a",
                    "endpoints": [{"url": "http://alertmanager.test"}],
                },
            )
        active.commit()
        yield active


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(grafana_import_api.router, prefix="/api")
    app.include_router(metrics_api.router, prefix="/api")
    app.include_router(event_sources_api.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def grafana(monkeypatch, dashboard):
    """Stand in for Grafana at the transport layer, keeping the real client."""

    state = {"dashboard": dashboard, "search_calls": 0, "fail": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"] is not None:
            return httpx.Response(state["fail"], json={"message": "no"})
        if "/api/search" in request.url.path:
            state["search_calls"] += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "uid": "mysql-overview",
                        "title": "MySQL Overview",
                        "folderTitle": "Databases",
                    }
                ],
            )
        return httpx.Response(200, json={"dashboard": state["dashboard"]})

    original = grafana_import_api._client

    def patched(session, row):
        client = original(session, row)
        return GrafanaDashboardClient(
            base_url=client.base_url,
            token=client.token,
            timeout_seconds=client.timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(grafana_import_api, "_client", patched)
    return state


@pytest.fixture
def thanos(monkeypatch):
    state = {"queries": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["queries"].append(request.url.params.get("query", ""))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1700000000, "0.83"]}],
                },
            },
        )

    def patched(session, source_id):
        row = session.exec(
            select(SourceThanosConfig).where(
                SourceThanosConfig.source_id == source_id
            )
        ).first()
        if row is None or not row.canonical_url:
            return None
        return ThanosClient(
            base_url=row.canonical_url,
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(grafana_import_api, "_thanos_for", patched)
    return state


def _configure(client, url: str = "http://10.0.0.5:3000", secret=None) -> None:
    payload = {"url": url, "timeout_seconds": 15}
    payload["secret"] = secret or {"action": "KEEP"}
    response = client.put(f"/api/event-sources/{SOURCE_ID}/grafana", json=payload)
    assert response.status_code == 200, response.text


def _configure_thanos(session) -> None:
    session.add(
        SourceThanosConfig(source_id=SOURCE_ID, canonical_url="http://thanos.test")
    )
    session.commit()


def _pass_the_test_gate(client, grafana) -> None:
    response = client.post(f"/api/event-sources/{SOURCE_ID}/grafana/test")
    assert response.json()["ok"] is True


class TestSavingTheAddress:
    def test_a_private_http_address_is_accepted(self, client, session) -> None:
        """The whole capability depends on this. Grafana is a read-only
        monitoring source on the internal network, and rejecting private
        addresses — the outbound rule — would make it unusable (CAP-13.1a)."""

        for address in (
            "http://10.0.0.5:3000",
            "http://192.168.1.7:3000",
            "http://grafana.monitoring.svc.cluster.local:3000",
        ):
            _configure(client, address)
            row = session.exec(select(SourceGrafanaConfig)).one()
            assert row.base_url == address

    def test_the_address_shows_up_on_the_source(self, client) -> None:
        _configure(client)

        config = client.get(f"/api/event-sources/{SOURCE_ID}").json()["config"]

        assert config["grafana"]["url"] == "http://10.0.0.5:3000"
        assert config["grafana"]["secret_configured"] is False

    def test_a_stored_credential_is_never_echoed_back(self, client) -> None:
        _configure(client, secret={"action": "REPLACE", "value": "glsa_secret_token"})

        body = client.get(f"/api/event-sources/{SOURCE_ID}").text

        assert "glsa_secret_token" not in body
        assert json.loads(body)["config"]["grafana"]["secret_configured"] is True

    def test_an_empty_address_removes_the_configuration(self, client, session) -> None:
        _configure(client)
        _configure(client, url="")

        assert session.exec(select(SourceGrafanaConfig)).all() == []

    def test_changing_the_address_invalidates_the_previous_test(
        self, client, session, grafana
    ) -> None:
        """A successful test against the old address says nothing about the new
        one, so the gate has to close again."""

        _configure(client)
        _pass_the_test_gate(client, grafana)
        assert session.exec(select(SourceGrafanaConfig)).one().last_test_status == "OK"

        _configure(client, url="http://10.0.0.9:3000")

        assert session.exec(select(SourceGrafanaConfig)).one().last_test_status is None

    @pytest.mark.parametrize(
        "secret",
        [
            {"action": "REPLACE", "value": "new-token"},
            {"action": "CLEAR"},
        ],
    )
    def test_changing_the_credential_invalidates_the_previous_test(
        self, client, session, grafana, secret
    ) -> None:
        _configure(client, secret={"action": "REPLACE", "value": "old-token"})
        _pass_the_test_gate(client, grafana)

        _configure(client, secret=secret)

        assert session.exec(select(SourceGrafanaConfig)).one().last_test_status is None
        assert (
            client.get(f"/api/event-sources/{SOURCE_ID}/grafana/dashboards").status_code
            == 409
        )

    def test_an_unknown_source_is_a_404(self, client) -> None:
        response = client.put(
            "/api/event-sources/src_nope/grafana",
            json={"url": "http://10.0.0.5:3000", "secret": {"action": "KEEP"}},
        )

        assert response.status_code == 404


class TestTheImportGate:
    def test_listing_dashboards_before_a_successful_test_is_refused(
        self, client, grafana
    ) -> None:
        """Review Q5. A gate on the endpoints, presented in the UI as nothing
        more than a disabled button and a hint — no draft state, no activation
        step, none of that vocabulary on the data-source side."""

        _configure(client)

        response = client.get(f"/api/event-sources/{SOURCE_ID}/grafana/dashboards")

        assert response.status_code == 409
        assert "先测试" in response.json()["detail"]

    def test_preview_before_a_successful_test_is_refused(
        self, client, grafana
    ) -> None:
        _configure(client)

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
            json={"dashboard_uid": "mysql-overview"},
        )

        assert response.status_code == 409

    def test_confirm_before_a_successful_test_is_refused(
        self, client, session
    ) -> None:
        _configure(client)

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item()]},
        )

        assert response.status_code == 409
        assert session.exec(select(MetricQueryTemplate)).all() == []

    def test_a_successful_test_opens_it(self, client, grafana) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)

        response = client.get(f"/api/event-sources/{SOURCE_ID}/grafana/dashboards")

        assert response.status_code == 200
        assert response.json()[0]["uid"] == "mysql-overview"

    def test_a_failed_test_records_a_safe_code_and_keeps_the_gate_shut(
        self, client, grafana, session
    ) -> None:
        _configure(client)
        grafana["fail"] = 401

        result = client.post(f"/api/event-sources/{SOURCE_ID}/grafana/test").json()

        assert result["ok"] is False
        assert result["code"] == "GRAFANA_AUTH_REQUIRED"
        row = session.exec(select(SourceGrafanaConfig)).one()
        assert row.last_test_status == "FAILED"
        # The address the user typed survives a failed test (CAP-01.7).
        assert row.base_url == "http://10.0.0.5:3000"
        assert (
            client.get(f"/api/event-sources/{SOURCE_ID}/grafana/dashboards").status_code
            == 409
        )

    def test_testing_without_an_address_says_so(self, client) -> None:
        response = client.post(f"/api/event-sources/{SOURCE_ID}/grafana/test")

        assert response.status_code == 409


class TestPreview:
    def test_it_never_writes_anything(
        self, client, session, grafana, thanos
    ) -> None:
        """**Reverse-validated.** CAP-13.2, the same rule aggregation rule
        previews follow.

        There is no server-side draft to keep: candidates go to the browser and
        come back as literals. A preview that wrote rows would leave templates
        behind for dashboards the user looked at and walked away from — and
        those rows would then be diffed against on the next re-import.
        """

        _configure(client)
        _configure_thanos(session)
        _pass_the_test_gate(client, grafana)

        for _ in range(3):
            response = client.post(
                f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
                json={"dashboard_uid": "mysql-overview"},
            )
            assert response.status_code == 200
            assert response.json()["candidates"]

        assert session.exec(select(MetricQueryTemplate)).all() == []
        assert session.exec(select(MetricTemplateOrigin)).all() == []
        assert session.exec(select(MetricTemplateBaseline)).all() == []

    def test_it_reports_every_classification(
        self, client, session, grafana, thanos
    ) -> None:
        _configure(client)
        _configure_thanos(session)
        _pass_the_test_gate(client, grafana)

        body = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
            json={"dashboard_uid": "mysql-overview"},
        ).json()

        statuses = {item["status"] for item in body["candidates"]}
        assert {"READY", "NEEDS_DECISION", "UNSUPPORTED"} <= statuses
        assert body["dashboard_title"] == "MySQL Overview"
        assert body["probed"] is True

    def test_without_a_history_address_nothing_is_reported_as_ready(
        self, client, session, grafana, thanos
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)

        body = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
            json={"dashboard_uid": "mysql-overview"},
        ).json()

        assert body["probed"] is False
        assert not [
            item for item in body["candidates"] if item["status"] == "READY"
        ]

    def test_it_says_how_many_curves_an_alert_can_draw(
        self, client, session, grafana, thanos
    ) -> None:
        """Before import, "which of these will actually be charted" was not a
        question anyone could have. Adding forty templates in one click makes it
        one, so the confirmation page has to answer it."""

        _configure(client)
        _pass_the_test_gate(client, grafana)

        body = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
            json={"dashboard_uid": "mysql-overview"},
        ).json()

        assert body["max_auxiliary_curves"] == 5
        assert body["enabled_template_count"] == 0

    def test_an_upstream_failure_is_reported_readably(
        self, client, session, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)
        grafana["fail"] = 404

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
            json={"dashboard_uid": "gone"},
        )

        assert response.status_code == 502
        assert "找不到" in response.json()["detail"]

    def test_a_malformed_uid_never_reaches_grafana(self, client, grafana) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/preview",
            json={"dashboard_uid": "../../secret"},
        )

        assert response.status_code == 422


class TestConfirm:
    def test_only_the_submitted_items_are_written(
        self, client, session, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item()]},
        )

        assert response.status_code == 200
        assert len(response.json()["created_template_ids"]) == 1
        assert len(session.exec(select(MetricQueryTemplate)).all()) == 1

    def test_an_empty_submission_writes_nothing(
        self, client, session, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": []},
        )

        assert response.status_code == 200
        assert session.exec(select(MetricQueryTemplate)).all() == []

    def test_confirming_twice_is_a_conflict_and_writes_one_row(
        self, client, session, grafana
    ) -> None:
        """**Reverse-validated.** A double click, or two tabs. The database
        constraint is what actually holds — a pre-check cannot see the other
        transaction."""

        _configure(client)
        _pass_the_test_gate(client, grafana)
        payload = {"items": [_item()]}

        first = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm", json=payload
        )
        second = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm", json=payload
        )

        assert first.status_code == 200
        assert second.status_code == 409
        assert len(session.exec(select(MetricTemplateOrigin)).all()) == 1
        assert len(session.exec(select(MetricQueryTemplate)).all()) == 1

    def test_a_retired_baseline_field_is_refused_and_not_stored(
        self, client, session, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)
        item = _item()
        item["baseline"] = {
            "value": 500,
            "direction": "HIGH_IS_BAD",
            "note": "",
        }

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [item]},
        )

        assert response.status_code == 422
        assert session.exec(select(MetricTemplateBaseline)).all() == []
        assert session.exec(select(MetricQueryTemplate)).all() == []

    def test_a_query_outside_the_scope_guard_is_refused(
        self, client, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item(final_promql="rate(up[30d])")]},
        )

        assert response.status_code == 422

    def test_an_unexpected_field_is_refused_rather_than_ignored(
        self, client, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)
        payload = _item()
        payload["surprise"] = 1

        response = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [payload]},
        )

        assert response.status_code == 422


class TestTheTemplateListAfterImport:
    def test_an_imported_template_carries_its_dashboard(
        self, client, session, grafana
    ) -> None:
        """So the list can say "from Grafana · MySQL Overview" instead of
        leaving imported and hand-written templates indistinguishable."""

        _configure(client)
        _pass_the_test_gate(client, grafana)
        client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item()]},
        )

        rows = client.get("/api/metric-templates").json()

        assert rows[0]["origin"]["dashboard_title"] == "MySQL Overview"
        assert rows[0]["origin"]["panel_id"] == 2
        assert "baseline" not in rows[0]
        assert rows[0]["source_scope"] == {
            "mode": "SELECTED",
            "source_ids": [SOURCE_ID],
        }

    def test_reimport_never_overwrites_a_user_changed_source_scope(
        self, client, session, grafana
    ) -> None:
        _configure(client)
        _pass_the_test_gate(client, grafana)
        first = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item()]},
        ).json()
        template_id = first["created_template_ids"][0]
        changed = client.patch(
            f"/api/metric-templates/{template_id}",
            json={"source_scope": {"mode": "ALL", "source_ids": []}},
        )
        assert changed.status_code == 200

        updated = client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item(final_promql="mysql_up == 1", template_id=template_id)]},
        )

        assert updated.status_code == 200
        [row] = client.get("/api/metric-templates").json()
        assert row["source_scope"] == {"mode": "ALL", "source_ids": []}

    def test_a_hand_written_template_has_no_origin(self, client) -> None:
        client.post(
            "/api/metric-templates",
            json={"name": "hand written", "promql": "up", "required_labels": []},
        )

        rows = client.get("/api/metric-templates").json()

        assert rows[0]["origin"] is None
        assert "baseline" not in rows[0]

    def test_deleting_a_template_leaves_no_orphan_rows(
        self, client, session, grafana
    ) -> None:
        """Current Origin and Source Scope side rows are removed explicitly."""

        _configure(client)
        _pass_the_test_gate(client, grafana)
        client.post(
            f"/api/event-sources/{SOURCE_ID}/grafana/import/confirm",
            json={"items": [_item()]},
        )
        template_id = session.exec(select(MetricQueryTemplate)).one().id

        assert client.delete(f"/api/metric-templates/{template_id}").status_code == 204

        assert session.exec(select(MetricTemplateOrigin)).all() == []
        assert session.exec(select(MetricTemplateSourceScope)).all() == []


def _item(
    *,
    final_promql: str = "mysql_up",
    template_id: int | None = None,
) -> dict:
    item = {
        "final_promql": final_promql,
        "name": "MySQL Overview · Up",
        "origin": {
            "dashboard_uid": "mysql-overview",
            "dashboard_title": "MySQL Overview",
            "panel_id": 2,
            "panel_title": "Up",
            "ref_id": "A",
        },
        "required_labels": [],
        "enabled": False,
        "display_unit": "",
        "order": 0,
    }
    if template_id is not None:
        item["template_id"] = template_id
    return item
