"""Keep implementation identifiers tied to capabilities, not delivery milestones."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
IMPLEMENTATION_ROOTS = (
    ROOT / "backend" / "app",
    ROOT / "backend" / "tests",
    ROOT / "scripts",
    ROOT / "frontend" / "src",
    ROOT / "operations-console" / "src",
)
IMPLEMENTATION_FILES = (
    ROOT / "backend" / "pyproject.toml",
    ROOT / "frontend" / "package.json",
)
MILESTONE_PATTERN = re.compile(r"(?<![A-Za-z0-9])" + "f" + r"29(?![A-Za-z0-9])", re.IGNORECASE)


def test_implementation_names_use_domain_language() -> None:
    offending_paths: list[str] = []
    offending_content: list[str] = []

    paths = [
        path
        for directory in IMPLEMENTATION_ROOTS
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    paths.extend(IMPLEMENTATION_FILES)

    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if MILESTONE_PATTERN.search(relative):
            offending_paths.append(relative)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if MILESTONE_PATTERN.search(content):
            offending_content.append(relative)

    assert not offending_paths, "milestone token found in implementation paths: " + ", ".join(
        offending_paths
    )
    assert not offending_content, "milestone token found in implementation content: " + ", ".join(
        offending_content
    )


def test_operations_console_uses_only_versioned_transport_and_components_do_not_fetch() -> None:
    frontend = ROOT / "operations-console" / "src"
    unversioned = re.compile(r'''["'`]\/api\/(?!v1(?:\/|["'`]))''')
    transport_import = re.compile(r'''from\s+["'][^"']*api/client["']''')
    api_violations: list[str] = []
    component_violations: list[str] = []
    for path in sorted(frontend.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        if unversioned.search(source):
            api_violations.append(str(path.relative_to(ROOT)))
        if "components" in path.parts and transport_import.search(source):
            component_violations.append(str(path.relative_to(ROOT)))
    assert api_violations == []
    assert component_violations == []
