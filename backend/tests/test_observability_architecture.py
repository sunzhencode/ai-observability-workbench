"""Observability import, retirement and language gates."""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def test_observability_modules_do_not_import_legacy_services_or_providers() -> None:
    roots = (
        APP / "domains" / "metrics",
        APP / "domains" / "model_channels",
        APP / "application" / "observability.py",
        APP / "api" / "v1" / "observability_routes",
        APP / "adapters" / "monitoring" / "thanos.py",
        APP / "adapters" / "monitoring" / "grafana.py",
        APP / "adapters" / "models",
    )
    violations: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if "app.services" in text or "app.providers" in text or "app.sources" in text:
                violations.append(str(path.relative_to(APP)))
    assert violations == []


def test_observability_candidate_does_not_revive_suggested_query_or_baseline() -> None:
    paths = tuple((APP / "domains" / "metrics").rglob("*.py")) + tuple(
        (APP / "application").glob("observability.py")
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "SuggestedQuery" not in text
    assert "MetricTemplateBaseline" not in text
    assert "BASELINE_BREACH" not in text


def test_observability_candidate_copy_avoids_banned_generic_language() -> None:
    banned = ("分析失败", "调查出错", "加载失败", "AI 看看", "根因已找到", "操作成功")
    roots = (
        APP / "domains" / "metrics",
        APP / "domains" / "model_channels",
        APP / "application" / "observability.py",
        APP / "api" / "v1" / "observability_routes",
        APP / "adapters" / "monitoring" / "thanos.py",
        APP / "adapters" / "monitoring" / "grafana.py",
        APP / "adapters" / "models",
    )
    violations: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            violations.extend(
                f"{path.relative_to(APP)}:{phrase}" for phrase in banned if phrase in text
            )
    assert violations == []


def test_observability_transport_composes_narrow_subdomain_route_modules() -> None:
    composition = (APP / "api" / "v1" / "observability.py").read_text(encoding="utf-8")
    route_root = APP / "api" / "v1" / "observability_routes"
    expected = {
        "metric_templates.py",
        "model_channels.py",
        "monitoring.py",
        "prompt_profiles.py",
    }

    assert "@router." not in composition
    assert expected <= {path.name for path in route_root.glob("*.py")}
    for name in expected:
        assert "@router." in (route_root / name).read_text(encoding="utf-8")
