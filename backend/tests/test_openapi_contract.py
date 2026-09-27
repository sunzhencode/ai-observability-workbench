"""Pin the HTTP contract of the really-mounted app.

``frontend/src/types.ts`` mirrors these schemas by hand, so a silent backend
contract change is invisible until it breaks in the browser.  This snapshot is
the drift signal: when it fails, either fix the backend or update the frontend
types and refresh the snapshot with

    ALERT_WORKBENCH_UPDATE_OPENAPI_SNAPSHOT=1 ../.venv/bin/python -m pytest \
        tests/test_openapi_contract.py

Unlike the other API tests, this one imports ``app.main`` so that an unmounted
router is a failure rather than dead code nobody notices.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).parent / "openapi_snapshot.json"

API_PACKAGE = "app.api"
API_DIR = Path(__file__).resolve().parents[1] / "app" / "api"


def _current_schema() -> dict:
    from app.main import app

    return json.loads(json.dumps(app.openapi(), sort_keys=True))


def _dump(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_openapi_contract_matches_snapshot() -> None:
    current = _current_schema()

    if os.environ.get("ALERT_WORKBENCH_UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(_dump(current), encoding="utf-8")
        pytest.skip("OpenAPI snapshot refreshed")

    assert SNAPSHOT.exists(), (
        "missing OpenAPI snapshot; regenerate with "
        "ALERT_WORKBENCH_UPDATE_OPENAPI_SNAPSHOT=1"
    )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    if current != expected:
        current_paths = set(current.get("paths", {}))
        expected_paths = set(expected.get("paths", {}))
        added = sorted(current_paths - expected_paths)
        removed = sorted(expected_paths - current_paths)
        pytest.fail(
            "OpenAPI contract changed.\n"
            f"  added paths: {added or '<none>'}\n"
            f"  removed paths: {removed or '<none>'}\n"
            "Update frontend/src/types.ts if the change is intentional, then "
            "refresh with ALERT_WORKBENCH_UPDATE_OPENAPI_SNAPSHOT=1."
        )


def test_every_api_router_is_mounted() -> None:
    """An unmounted router is dead code; keep it from accumulating again."""
    from app.main import app

    # ``app.routes`` holds one nested router object per ``include_router`` call,
    # so the reachable paths have to come from the generated schema instead.
    mounted = set(app.openapi()["paths"])
    unmounted: list[str] = []

    for module_path in sorted(API_DIR.glob("*.py")):
        if module_path.stem == "__init__":
            continue
        module = importlib.import_module(f"{API_PACKAGE}.{module_path.stem}")
        router = getattr(module, "router", None)
        if router is None:
            continue
        paths = {f"/api{route.path}" for route in router.routes}
        if paths and not (paths & mounted):
            unmounted.append(module_path.stem)

    assert not unmounted, (
        f"router modules not mounted in app.main: {unmounted}. "
        "Mount them or delete them."
    )
