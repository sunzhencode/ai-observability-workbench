"""Observability persistence and use-case contracts on the isolated platform."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, select

from app.adapters.persistence.observability import (
    GrafanaTemplateOriginRecord,
    MetricBackfillRunRecord,
    SqlAlchemyObservabilityStore,
)
from app.adapters.persistence.sources import AlertRecord, IncidentRecord, SqlAlchemySourceStore
from app.application.observability import (
    GrafanaImportSelection,
    BackfillSourceMetrics,
    MetricTemplateDraft,
    ModelChannelInput,
    MonitoringConnectionDraft,
    PreviewGrafanaImport,
    ReadMetricEvidence,
)
from app.application.sources import EndpointDraft, SourceDraft
from app.domains.metrics.models import MetricReadStatus
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database

UTC = timezone.utc


def _stores(tmp_path: Path):
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    secrets: dict[str, str] = {}

    def encrypt(value: str) -> str:
        key = f"encrypted-{len(secrets) + 1}"
        secrets[key] = value
        return key

    source = SqlAlchemySourceStore(sessions)
    source.create_source(
        SourceDraft("src-a", "Primary", (EndpointDraft(0, "https://am.invalid"),)),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    store = SqlAlchemyObservabilityStore(
        sessions, encrypt_secret=encrypt, decrypt_secret=secrets.__getitem__
    )
    return source, store, sessions, engine


def test_monitoring_config_test_gate_and_secret_no_echo(tmp_path: Path) -> None:
    _source, store, _sessions, _engine = _stores(tmp_path)
    draft = store.save_monitoring_connection(
        MonitoringConnectionDraft(
            "src-a",
            "THANOS",
            "http://10.0.0.8:9090",
            secret_action="REPLACE",
            secret_value="test-token",
        ),
        expected_version=None,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert draft.state == "DRAFT" and draft.secret_configured is True
    assert "test-token" not in repr(draft)
    assert store.load_monitoring_secret("src-a", "THANOS").secret == "test-token"
    active = store.record_monitoring_test(
        "src-a",
        "THANOS",
        expected_version=1,
        ok=True,
        safe_error_code="OK",
        now=datetime(2026, 8, 12, 1, tzinfo=UTC),
    )
    assert active.state == "ACTIVE"


def test_grafana_preview_is_read_only_and_confirm_preserves_origin(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    store.save_monitoring_connection(
        MonitoringConnectionDraft("src-a", "GRAFANA", "http://grafana.internal"),
        expected_version=None,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    class Grafana:
        async def get_dashboard(self, uid: str):
            return {
                "title": "Kubernetes",
                "panels": [
                    {
                        "id": 3,
                        "title": "CPU",
                        "datasource": {"type": "prometheus"},
                        "targets": [{"refId": "A", "expr": "sum(up)"}],
                    }
                ],
            }

    candidates = __import__("asyncio").run(
        PreviewGrafanaImport().execute(Grafana(), dashboard_uid="kube-main")
    )
    assert store.list_metric_templates() == ()
    created = store.import_grafana_templates(
        "src-a",
        (
            GrafanaImportSelection(candidates[0], "CPU availability", "sum(up)", 10),
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert created[0].origin_kind == "GRAFANA"
    assert created[0].grafana_origin is not None
    assert created[0].grafana_origin.dashboard_uid == "kube-main"
    assert created[0].grafana_origin.base_url == "http://grafana.internal"
    with sessions() as session:
        origin = session.scalar(select(GrafanaTemplateOriginRecord))
        assert origin is not None and origin.imported_promql == "sum(up)"


def test_model_channel_save_activates_and_test_result_is_non_blocking(tmp_path: Path) -> None:
    _source, store, _sessions, _engine = _stores(tmp_path)
    draft = store.create_model_channel(
        ModelChannelInput(
            "Primary model",
            "OPENAI_COMPATIBLE",
            "https://models.example.invalid/v1",
            "gpt-compatible",
            api_key_action="REPLACE",
            api_key_value="test-key",
        ),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert draft.state == "ACTIVE" and draft.enabled is True
    assert store.active_model_channel() is not None
    assert store.active_model_channel().model == "gpt-compatible"  # type: ignore[union-attr]
    tested = store.record_model_test(
        draft.id,
        expected_revision=1,
        ok=False,
        safe_error_code="AUTH_FAILED",
        now=datetime(2026, 8, 12, 1, tzinfo=UTC),
    )
    assert tested.state == "ACTIVE" and tested.enabled is True
    assert tested.last_test_code == "AUTH_FAILED"
    assert "test-key" not in repr(tested)
    next_active = store.update_model_channel(
        draft.id,
        ModelChannelInput(
            "Primary model",
            "OPENAI_COMPATIBLE",
            "https://models.example.invalid/v1",
            "next-model",
        ),
        expected_revision=1,
        now=datetime(2026, 8, 12, 3, tzinfo=UTC),
    )
    assert next_active.state == "ACTIVE" and next_active.enabled is True
    assert next_active.revision_no == 2
    assert store.load_model_secret(draft.id).view.model == "next-model"
    assert store.active_model_channel() is not None
    assert store.active_model_channel().model == "next-model"  # type: ignore[union-attr]
    assert store.load_model_secret_revision(draft.id, 2).view.model == "next-model"
    store.set_model_channel_enabled(
        draft.id, enabled=False, now=datetime(2026, 8, 12, 4, tzinfo=UTC)
    )
    try:
        store.load_model_secret_revision(draft.id, 2)
    except RuntimeError as exc:
        assert str(exc) == "MODEL_CHANNEL_REVISION_NOT_ACTIVE"
    else:
        raise AssertionError("disabled frozen model revision remained callable")


def test_model_channel_persists_explicit_provider_profile_and_canonical_target(
    tmp_path: Path,
) -> None:
    _source, store, _sessions, _engine = _stores(tmp_path)
    saved = store.create_model_channel(
        ModelChannelInput(
            "Kimi",
            "OPENAI_COMPATIBLE",
            "https://wrong.example.invalid/v1",
            "kimi-k2.5",
            api_key_action="REPLACE",
            api_key_value="test-key",
            provider_id="MOONSHOT",
        ),
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )

    assert saved.provider_id == "MOONSHOT"
    assert saved.protocol_profile == "CHAT_COMPLETIONS"
    assert saved.support_level == "REVIEWED"
    assert saved.provider_profile_id is not None
    assert saved.base_url == "https://api.moonshot.cn/v1"

    updated = store.update_model_channel(
        saved.id,
        ModelChannelInput(
            "Kimi",
            "OPENAI_COMPATIBLE",
            "https://also-wrong.example.invalid",
            "gpt-5.5",
            provider_id="OPENAI",
        ),
        expected_revision=1,
        now=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )
    assert updated.provider_id == "OPENAI"
    assert updated.protocol_profile == "RESPONSES"
    assert updated.base_url == "https://api.openai.com/v1"


async def test_backfill_is_idempotent_reconstructed_and_live_poll_can_supersede(
    tmp_path: Path,
) -> None:
    source, store, sessions, _engine = _stores(tmp_path)
    store.save_monitoring_connection(
        MonitoringConnectionDraft("src-a", "THANOS", "http://thanos.internal"),
        expected_version=None,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    store.record_monitoring_test(
        "src-a",
        "THANOS",
        expected_version=1,
        ok=True,
        safe_error_code="OK",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    class Thanos:
        async def query_alerts(self, start, end, step_seconds):
            return {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "ALERTS",
                            "alertstate": "firing",
                            "alertname": "TargetDown",
                            "severity": "warning",
                        },
                        "values": [[1_786_496_460, "1"], [1_786_496_520, "1"]],
                    }
                ],
            }

    use_case = BackfillSourceMetrics(store)
    await use_case.execute(
        "src-a", Thanos(), expected_connection_version=1, now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    await use_case.execute(
        "src-a", Thanos(), expected_connection_version=1, now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    with sessions() as session:
        alerts = list(session.scalars(select(AlertRecord)))
        incidents = list(session.scalars(select(IncidentRecord)))
        backfill_runs = list(
            session.scalars(
                select(MetricBackfillRunRecord).order_by(MetricBackfillRunRecord.id)
            )
        )
        assert len(alerts) == len(incidents) == 1
        assert alerts[0].origin == "BACKFILL"
        assert alerts[0].evidence_completeness == "RECONSTRUCTED"
        assert alerts[0].source_state == "RECOVERED"
        assert incidents[0].source_state == "RECOVERED"
        assert len(backfill_runs) == 2
        assert backfill_runs[-1].requested_hours == 24
        assert backfill_runs[-1].effective_hours == 24
        assert backfill_runs[-1].truncated_reason is None
        assert backfill_runs[-1].alerts_reconstructed == 1
        raw = __import__("json").loads(alerts[0].raw_json)

    source.seed_alert_for_characterization(
        source_id="src-a",
        raw={**raw, "status": {"state": "active"}},
        observed_at=datetime(2026, 8, 12, 1, tzinfo=UTC),
    )
    with sessions() as session:
        alert = session.scalar(select(AlertRecord))
        assert alert is not None
        assert alert.origin == "LIVE_POLL"
        assert alert.evidence_completeness == "COMPLETE"
        assert alert.source_state == "FIRING"


async def test_backfill_excludes_watchdog_and_active_live_label_identity(
    tmp_path: Path,
) -> None:
    source, store, sessions, _engine = _stores(tmp_path)
    store.save_monitoring_connection(
        MonitoringConnectionDraft("src-a", "THANOS", "http://thanos.internal"),
        expected_version=None,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    store.record_monitoring_test(
        "src-a",
        "THANOS",
        expected_version=1,
        ok=True,
        safe_error_code="OK",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    source.seed_alert_for_characterization(
        source_id="src-a",
        raw={
            "fingerprint": "alertmanager-live-fingerprint",
            "labels": {
                "alertname": "TargetDown",
                "severity": "warning",
                "instance": "node-a",
                "service": "checkout-api",
                "environment": "local",
                "source": "local-prometheus",
            },
            "annotations": {"summary": "node-a is down"},
            "startsAt": "2026-08-12T00:00:00Z",
            "status": {"state": "active"},
        },
        observed_at=datetime(2026, 8, 12, 1, tzinfo=UTC),
    )

    class Thanos:
        async def query_alerts(self, start, end, step_seconds):
            return {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "ALERTS",
                            "alertstate": "firing",
                            "alertname": "TargetDown",
                            "severity": "warning",
                            "instance": "node-a",
                            "service": "checkout-api",
                        },
                        "values": [[1_786_496_460, "1"], [1_786_496_520, "1"]],
                    },
                    {
                        "metric": {
                            "__name__": "ALERTS",
                            "alertstate": "firing",
                            "alertname": "Watchdog",
                            "severity": "none",
                        },
                        "values": [[1_786_496_460, "1"], [1_786_496_520, "1"]],
                    },
                    {
                        "metric": {
                            "__name__": "ALERTS",
                            "alertstate": "firing",
                            "alertname": "TargetDown",
                            "severity": "warning",
                            "instance": "node-b",
                            "service": "checkout-api",
                        },
                        "values": [[1_786_496_460, "1"], [1_786_496_520, "1"]],
                    },
                ],
            }

    result = await BackfillSourceMetrics(store).execute(
        "src-a",
        Thanos(),
        expected_connection_version=1,
        now=datetime(2026, 8, 12, 2, tzinfo=UTC),
    )

    assert result.reconstructed == 1
    with sessions() as session:
        alerts = list(session.scalars(select(AlertRecord).order_by(AlertRecord.id)))
        incidents = list(session.scalars(select(IncidentRecord).order_by(IncidentRecord.id)))
        backfill_run = session.scalar(
            select(MetricBackfillRunRecord).order_by(MetricBackfillRunRecord.id.desc())
        )
        assert [(item.alertname, item.origin, item.source_state) for item in alerts] == [
            ("TargetDown", "LIVE_POLL", "FIRING"),
            ("TargetDown", "BACKFILL", "RECOVERED"),
        ]
        assert [item.source_state for item in incidents] == ["FIRING", "RECOVERED"]
        assert backfill_run is not None
        assert backfill_run.alerts_reconstructed == 1


async def test_main_and_template_curves_use_thanos_only_and_keep_empty_typed(tmp_path: Path) -> None:
    source, store, _sessions, _engine = _stores(tmp_path)
    source.seed_alert_for_characterization(
        source_id="src-a",
        raw={
            "fingerprint": "cpu-high",
            "labels": {
                "alertname": "CPUHigh",
                "severity": "warning",
                "cluster": 'prod"blue',
            },
            "annotations": {},
            "startsAt": "2026-08-12T00:00:00Z",
        },
        observed_at=datetime(2026, 8, 12, 0, 5, tzinfo=UTC),
    )
    alert_id = source.list_alerts()[0].id
    store.create_metric_template(
        MetricTemplateDraft(
            "Availability",
            'up{cluster="{{cluster}}"}',
            True,
            10,
            ("src-a",),
            required_labels=("cluster",),
        ),
        origin_kind="MANUAL",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    class Thanos:
        calls: list[str] = []

        async def alert_rules(self, alertname: str):
            return ({"type": "alerting", "name": alertname, "query": "rate(cpu_total[5m]) > 0.5"},)

        async def query_range(self, query, start, end, step_seconds):
            self.calls.append(query)
            return {
                "resultType": "matrix",
                "result": []
                if query.startswith("up{")
                else [{"metric": {}, "values": [[1, "1"]]}],
            }

    thanos = Thanos()
    evidence = await ReadMetricEvidence(store).execute(alert_id, thanos)
    assert thanos.calls == [
        "rate(cpu_total[5m])",
        'up{cluster="prod\\"blue"}',
    ]
    assert [curve.kind for curve in evidence.curves] == ["MAIN", "TEMPLATE"]
    assert evidence.curves[1].result.status is MetricReadStatus.EMPTY_NO_DATA


async def test_generator_url_is_parsed_without_network_when_rule_catalog_is_empty(
    tmp_path: Path,
) -> None:
    source, store, _sessions, _engine = _stores(tmp_path)
    source.seed_alert_for_characterization(
        source_id="src-a",
        raw={
            "fingerprint": "fallback",
            "labels": {"alertname": "RuleCatalogMissing", "severity": "warning"},
            "annotations": {},
            "generatorURL": (
                "http://prometheus.internal/graph?"
                "g0.expr=rate%28requests_total%5B5m%5D%29%20%3E%2010"
            ),
            "startsAt": "2026-08-12T00:00:00Z",
        },
        observed_at=datetime(2026, 8, 12, 0, 5, tzinfo=UTC),
    )
    alert_id = source.list_alerts()[0].id

    class Thanos:
        calls: list[str] = []

        async def alert_rules(self, alertname: str):
            return ()

        async def query_range(self, query, start, end, step_seconds):
            self.calls.append(query)
            return {"resultType": "matrix", "result": []}

    thanos = Thanos()
    evidence = await ReadMetricEvidence(store).execute(alert_id, thanos)
    assert thanos.calls == ["rate(requests_total[5m])"]
    assert evidence.curves[0].result.status is MetricReadStatus.EMPTY_NO_DATA
    assert "MAIN_EXPRESSION_UNAVAILABLE" not in evidence.failures


def test_observability_migration_has_no_baseline_or_suggested_query_tables(tmp_path: Path) -> None:
    _source, _store, _sessions, engine = _stores(tmp_path)
    tables = set(inspect(engine).get_table_names())
    assert {
        "monitoring_connection",
        "metric_template",
        "grafana_template_origin",
        "model_channel",
        "model_channel_revision",
        "model_channel_audit",
    } <= tables
    assert not any("baseline" in name or "suggested" in name for name in tables)
