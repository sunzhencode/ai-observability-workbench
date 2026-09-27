from __future__ import annotations

import ast
from pathlib import Path


def test_framework_types_do_not_cross_domain_or_application_boundary() -> None:
    root = Path(__file__).parents[1] / "app"
    checked = [root / "domains" / "investigations", root / "application" / "investigator_runtime.py"]
    for target in checked:
        paths = target.rglob("*.py") if target.is_dir() else (target,)
        for path in paths:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                assert not any(
                    name == "pydantic_ai" or name.startswith("pydantic_ai.")
                    or name == "pydantic_ai_harness" or name.startswith("pydantic_ai_harness.")
                    for name in names
                ), f"framework import crossed boundary: {path}:{node.lineno}"
