"""Platform bootstrap contracts.

The candidate factory deliberately coexists with ``app.main.app``.  These
tests pin platform behaviour without switching any current business route.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.bootstrap import create_platform_app, default_platform_wiring
from app.platform.errors import SafeApiError
from app.platform.health import ReadinessProbe, ReadinessProbeResult
from app.platform.redaction import redact_sensitive
from app.platform.utc import to_utc_iso, utc_now


def test_candidate_can_serve_the_compiled_interface_without_shadowing_api(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "dist"
    assets = distribution / "assets"
    assets.mkdir(parents=True)
    (distribution / "index.html").write_text(
        "<!doctype html><html><head></head><body><main>operator</main></body></html>",
        encoding="utf-8",
    )
    (distribution / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets / "app-abc123.js").write_text("export {};", encoding="utf-8")
    app = create_platform_app(frontend_dist=distribution)

    with TestClient(app) as client:
        root = client.get("/")
        deep_link = client.get("/settings/model-channels")
        asset = client.get("/assets/app-abc123.js")
        favicon = client.get("/favicon.svg")
        missing_asset = client.get("/assets/missing.js")
        unknown_ui_route = client.get("/nope")
        unknown_api_route = client.get("/api/v1/nope")
        health = client.get("/health/live")

    assert root.text == deep_link.text
    assert "<main>operator</main>" in root.text
    assert '<meta name="csrf-token" content="' in root.text
    assert root.headers["cache-control"] == "no-cache"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert favicon.headers["cache-control"] == "public, max-age=3600"
    assert missing_asset.status_code == 404 and missing_asset.content == b""
    assert "<main>operator</main>" in unknown_ui_route.text
    assert unknown_api_route.status_code == 404
    assert health.json()["status"] == "alive"


def test_candidate_rejects_an_incomplete_compiled_interface(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="compiled operator interface is incomplete"):
        create_platform_app(frontend_dist=tmp_path / "missing")


def test_browser_mutations_require_trusted_host_same_origin_and_page_token(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "dist"
    (distribution / "assets").mkdir(parents=True)
    (distribution / "index.html").write_text(
        "<!doctype html><html><head></head><body>operator</body></html>",
        encoding="utf-8",
    )
    router = APIRouter()

    @router.post("/api/v1/mutate")
    async def mutate() -> dict[str, bool]:
        return {"updated": True}

    wiring = default_platform_wiring(routers=(router,))
    app = create_platform_app(wiring=wiring, frontend_dist=distribution)

    with TestClient(app) as client:
        page = client.get("/")
        match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
        assert match is not None
        token = match.group(1)
        missing_token = client.post("/api/v1/mutate")
        bad_origin = client.post(
            "/api/v1/mutate",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
        )
        accepted = client.post(
            "/api/v1/mutate",
            headers={"Origin": "http://testserver", "X-CSRF-Token": token},
        )
        untrusted_host = client.get(
            "/health/live", headers={"Host": "evil.example"}
        )

    assert len(token) >= 32
    assert missing_token.status_code == 403
    assert missing_token.json()["error"]["code"] == "CSRF_TOKEN_INVALID"
    assert bad_origin.status_code == 403
    assert bad_origin.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert accepted.status_code == 200
    assert accepted.json() == {"updated": True}
    assert untrusted_host.status_code == 400
    assert untrusted_host.json()["error"]["code"] == "HOST_NOT_ALLOWED"
    assert "csrf-token" not in json.dumps(app.openapi())


def test_non_loopback_host_must_be_explicit_and_logs_full_permission_warning(
    tmp_path: Path,
    caplog,
) -> None:
    distribution = tmp_path / "dist"
    (distribution / "assets").mkdir(parents=True)
    (distribution / "index.html").write_text(
        "<!doctype html><html><head></head><body>operator</body></html>",
        encoding="utf-8",
    )
    caplog.set_level("WARNING", logger="incident_operations.platform.security")
    app = create_platform_app(
        frontend_dist=distribution,
        trusted_hosts=("192.168.10.20",),
    )

    with TestClient(app) as client:
        response = client.get(
            "/health/live", headers={"Host": "192.168.10.20:8000"}
        )

    assert response.status_code == 200
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "full operator permissions" in rendered


def test_factory_does_not_replace_the_current_default_app() -> None:
    from app.main import app as current_app

    candidate = create_platform_app()

    assert candidate is not current_app
    assert candidate.title == "AI Incident Operations Platform"
    assert "/api/incidents" in current_app.openapi()["paths"]
    assert "/api/incidents" not in candidate.openapi()["paths"]


def test_lifespan_runs_hooks_once_and_shutdown_in_reverse_order() -> None:
    events: list[str] = []

    async def start_one() -> None:
        events.append("start-one")

    async def start_two() -> None:
        events.append("start-two")

    async def stop_one() -> None:
        events.append("stop-one")

    async def stop_two() -> None:
        events.append("stop-two")

    wiring = default_platform_wiring(
        startup_hooks=(start_one, start_two),
        shutdown_hooks=(stop_one, stop_two),
    )
    app = create_platform_app(wiring=wiring)

    with TestClient(app) as client:
        assert events == ["start-one", "start-two"]
        assert client.get("/health/ready").json()["status"] == "ready"

    assert events == ["start-one", "start-two", "stop-two", "stop-one"]


def test_factory_rejects_multiple_workers() -> None:
    with pytest.raises(RuntimeError, match="single Uvicorn worker"):
        create_platform_app(worker_count=2)
    with pytest.raises(RuntimeError, match="single Uvicorn worker"):
        create_platform_app(worker_count=True)


def test_liveness_readiness_and_metrics_are_separate_contracts() -> None:
    app = create_platform_app()

    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        metrics = client.get("/metrics")

    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    assert live.json()["checked_at"].endswith("Z")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    assert "incident_operations_platform_ready 1" in metrics.text
    assert 'incident_operations_http_requests_total{method="GET",route="/health/live",status="200"}' in metrics.text


def test_each_factory_has_isolated_runtime_and_metric_wiring() -> None:
    first = create_platform_app()
    second = create_platform_app()

    with TestClient(first) as client:
        client.get("/health/live")
        first_metrics = client.get("/metrics").text
    with TestClient(second) as client:
        second_metrics = client.get("/metrics").text

    assert 'route="/health/live"' in first_metrics
    assert 'route="/health/live"' not in second_metrics


def test_readiness_failure_is_typed_and_never_echoes_raw_exception() -> None:
    secret = "upstream-token-that-must-not-escape"

    async def failing_check() -> ReadinessProbeResult:
        raise RuntimeError(f"database unavailable token={secret}")

    probe = ReadinessProbe(
        name="database",
        check=failing_check,
        failure_code="DATABASE_READINESS_FAILED",
    )
    wiring = default_platform_wiring(readiness_probes=(probe,))
    app = create_platform_app(wiring=wiring)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == [
        {
            "name": "database",
            "status": "not_ready",
            "code": "DATABASE_READINESS_FAILED",
        }
    ]
    assert secret not in response.text
    assert "database unavailable" not in response.text


def test_readiness_rejects_unbounded_or_non_safe_labels() -> None:
    async def ok_check() -> ReadinessProbeResult:
        return ReadinessProbeResult()

    with pytest.raises(ValueError, match="lower-case"):
        ReadinessProbe(name="Database URL", check=ok_check, failure_code="FAILED")
    with pytest.raises(ValueError, match="safe code"):
        ReadinessProbe(
            name="database",
            check=ok_check,
            failure_code="failed token=do-not-export",
        )


def test_request_id_safe_error_and_request_log_never_include_query_secret(caplog) -> None:
    app = create_platform_app()

    @app.get("/test/safe-error")
    async def safe_error() -> None:
        raise SafeApiError(status_code=409, code="EXPECTED_CONFLICT", message="刷新后重试")

    caplog.set_level("INFO", logger="incident_operations.platform.http")
    with TestClient(app) as client:
        response = client.get(
            "/test/safe-error?api_key=do-not-log-this",
            headers={"X-Request-ID": "request-123"},
        )

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.json() == {
        "error": {
            "code": "EXPECTED_CONFLICT",
            "message": "刷新后重试",
            "request_id": "request-123",
            "details": {},
        }
    }
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "do-not-log-this" not in rendered
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["request_id"] == "request-123"
    assert payload["route"] == "/test/safe-error"
    assert "query" not in payload


def test_invalid_request_id_is_replaced_with_a_bounded_server_value() -> None:
    app = create_platform_app()

    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "!" * 512})

    request_id = response.headers["X-Request-ID"]
    assert request_id != "!" * 512
    assert 16 <= len(request_id) <= 64


def test_unexpected_error_response_and_log_expose_only_exception_type(caplog) -> None:
    app = create_platform_app()
    secret = "raw-upstream-secret-must-not-escape"

    @app.get("/test/unexpected-error")
    async def unexpected_error() -> None:
        raise RuntimeError(f"connection failed token={secret}")

    caplog.set_level("ERROR", logger="incident_operations.platform.errors")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/test/unexpected-error", headers={"X-Request-ID": "request-500"}
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "request-500"
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "服务未能完成请求，请使用 request_id 查询本地日志",
        "request_id": "request-500",
        "details": {},
    }
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in rendered
    assert secret not in rendered + response.text
    assert "connection failed" not in rendered + response.text


def test_utc_helpers_never_emit_naive_or_offset_datetimes() -> None:
    assert to_utc_iso(datetime(2026, 8, 10, 8, 30, 0)) == "2026-08-10T08:30:00Z"
    assert to_utc_iso(
        datetime(2026, 8, 10, 16, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    ) == "2026-08-10T08:30:00Z"
    assert utc_now().tzinfo == timezone.utc


def test_platform_redaction_is_recursive_and_non_mutating() -> None:
    original = {
        "safe": "visible",
        "nested": {"api_token": "secret-value"},
        "items": [{"password": "secret-value"}],
    }

    redacted = redact_sensitive(original)

    assert redacted == {
        "safe": "visible",
        "nested": {"api_token": "[REDACTED]"},
        "items": [{"password": "[REDACTED]"}],
    }
    assert original["nested"]["api_token"] == "secret-value"
