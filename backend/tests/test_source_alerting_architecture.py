"""Source-alerting architecture and professional-language negative gates."""

from __future__ import annotations

from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def test_source_alerting_candidate_copy_avoids_banned_generic_language() -> None:
    banned = (
        "分析失败",
        "调查出错",
        "加载失败",
        "AI 看看",
        "根因已找到",
        "操作成功",
    )
    roots = (
        APP / "domains" / "sources",
        APP / "domains" / "alerting",
        APP / "application" / "sources.py",
        APP / "adapters" / "monitoring",
        APP / "adapters" / "persistence" / "sources.py",
        APP / "api" / "v1" / "sources.py",
    )
    violations: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for phrase in banned:
                if phrase in text:
                    violations.append(f"{path.relative_to(APP)}:{phrase}")
    assert violations == []


def test_deterministic_source_alerting_layers_have_no_network_imports() -> None:
    paths = tuple((APP / "domains" / "sources").rglob("*.py")) + tuple(
        (APP / "domains" / "alerting").rglob("*.py")
    ) + (APP / "application" / "sources.py",)
    violations = [
        str(path.relative_to(APP))
        for path in paths
        if "import httpx" in path.read_text(encoding="utf-8")
    ]
    assert violations == []
