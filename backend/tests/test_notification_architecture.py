"""Notification dependency, language and side-effect boundary gates."""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def test_notification_candidate_does_not_import_legacy_business_modules() -> None:
    roots = (
        APP / "domains" / "notifications",
        APP / "application" / "notifications.py",
        APP / "adapters" / "notifications",
        APP / "adapters" / "persistence" / "notifications.py",
        APP / "api" / "v1" / "notifications.py",
    )
    violations: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if "app.services" in text or "app.providers" in text or "app.models" in text or "sqlmodel" in text:
                violations.append(str(path.relative_to(APP)))
    assert violations == []


def test_notification_candidate_has_no_network_import_in_domain_application_or_persistence() -> None:
    paths = tuple((APP / "domains" / "notifications").rglob("*.py")) + (
        APP / "application" / "notifications.py",
        APP / "adapters" / "persistence" / "notifications.py",
    )
    assert [
        str(path.relative_to(APP))
        for path in paths
        if "import httpx" in path.read_text(encoding="utf-8")
        or "import smtplib" in path.read_text(encoding="utf-8")
    ] == []


def test_notification_candidate_avoids_banned_generic_language() -> None:
    banned = ("分析失败", "调查出错", "加载失败", "AI 看看", "根因已找到", "操作成功")
    paths = tuple((APP / "domains" / "notifications").rglob("*.py")) + tuple(
        (APP / "adapters" / "notifications").rglob("*.py")
    ) + (
        APP / "application" / "notifications.py",
        APP / "api" / "v1" / "notifications.py",
    )
    violations = [
        f"{path.relative_to(APP)}:{phrase}"
        for path in paths
        for phrase in banned
        if phrase in path.read_text(encoding="utf-8")
    ]
    assert violations == []
