"""Static import guard for the platform dependency layers."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
NEW_LAYERS = (ROOT / "bootstrap", ROOT / "platform")
FORBIDDEN_PREFIXES = (
    "app.models",
    "app.registry_models",
    "app.services",
    "app.sources",
    "sqlmodel",
)
PERSISTENCE_FORBIDDEN_PREFIXES = (
    "httpx",
    "requests",
    "app.providers",
    "app.sources",
)


def test_bootstrap_and_platform_do_not_reach_into_legacy_business_layers() -> None:
    violations: list[str] = []
    for directory in NEW_LAYERS:
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.startswith("app.api") and not name.startswith("app.api.v1"):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
                    if name.startswith(FORBIDDEN_PREFIXES):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
                    if "persistence" in path.parts and name.startswith(
                        PERSISTENCE_FORBIDDEN_PREFIXES
                    ):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:network:{name}"
                        )

    assert not violations, "new platform layer imports legacy business code:\n" + "\n".join(violations)
