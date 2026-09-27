"""Incident dependency, language and side-effect boundary gates."""

from __future__ import annotations

from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"


def _paths() -> tuple[Path, ...]:
    return tuple((APP / "domains" / "incidents").rglob("*.py")) + (
        APP / "application" / "incidents.py",
        APP / "adapters" / "persistence" / "incidents.py",
        APP / "api" / "v1" / "incidents.py",
    )


def test_incident_candidate_does_not_import_legacy_business_modules() -> None:
    banned = ("app.services", "app.models", "app.registry_models", "sqlmodel")
    violations = [
        f"{path.relative_to(APP)}:{token}"
        for path in _paths()
        for token in banned
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_incident_domain_application_and_persistence_have_no_network_imports() -> None:
    paths = tuple((APP / "domains" / "incidents").rglob("*.py")) + (
        APP / "application" / "incidents.py",
        APP / "adapters" / "persistence" / "incidents.py",
    )
    banned = ("import httpx", "import requests", "import smtplib")
    violations = [
        f"{path.relative_to(APP)}:{token}"
        for path in paths
        for token in banned
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_incident_candidate_avoids_banned_generic_language() -> None:
    banned = ("分析失败", "调查出错", "加载失败", "AI 看看", "根因已找到", "操作成功")
    violations = [
        f"{path.relative_to(APP)}:{phrase}"
        for path in _paths()
        for phrase in banned
        if phrase in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_only_interactive_api_adapter_can_mint_operator_capability() -> None:
    allowed_mint = {
        APP / "domains" / "incidents" / "actors.py",
        APP / "api" / "v1" / "operator_witness.py",
    }
    allowed_witness = {
        APP / "api" / "v1" / "operator_witness.py",
        APP / "api" / "v1" / "incidents.py",
            APP / "api" / "v1" / "investigations.py",
            APP / "api" / "v1" / "unified_investigations.py",
            APP / "api" / "v1" / "noise.py",
    }
    production = tuple(APP.rglob("*.py"))
    mint_violations = [
        str(path.relative_to(APP))
        for path in production
        if path not in allowed_mint
        and (
            "_mint_interactive_operator" in path.read_text(encoding="utf-8")
            or "InteractiveOperatorActor(" in path.read_text(encoding="utf-8")
        )
    ]
    witness_violations = [
        str(path.relative_to(APP))
        for path in production
        if path not in allowed_witness
        and "OperatorWitness" in path.read_text(encoding="utf-8")
    ]
    assert mint_violations == []
    assert witness_violations == []


def test_poll_worker_and_ai_are_not_wired_interactive_incident_commands() -> None:
    allowed = {
        APP / "application" / "incidents.py",
        APP / "api" / "v1" / "incidents.py",
        APP / "bootstrap" / "job_platform.py",
    }
    violations = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if path not in allowed
        and any(
            capability in path.read_text(encoding="utf-8")
            for capability in (
                "IncidentResponseCommands",
                "IncidentCollaborationCommands",
            )
        )
    ]
    assert violations == []
