"""`app/f20_models.py` is a fossil, and this is what stops it becoming a path.

The models moved to `registry_models.py` because the old name claimed F20 while
holding F20 through F27. The old path could not simply be deleted: the
`from app.f20_models import ...` lines **inside applied migration functions** are
part of those migrations' checksums, so rewriting them would make every existing
database refuse to start.

That leaves a shim, and a shim with no guard is exactly the "second way to reach
the same thing" this repository keeps deleting (the legacy poll branch in F21,
the arbitrary-URL Thanos helper in F27). One import at a time, everyone ends up
back on the old name and the rename bought nothing.

So: only `migrations.py` may use it, and that is asserted rather than agreed.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
LEGACY_MODULE = "app.f20_models"

#: `migrations.py` because its import lines are inside frozen checksums, the
#: shim because it *is* the path, and this file because checking that the shim
#: still re-exports the right names means importing it.
ALLOWED = {
    BACKEND / "app" / "migrations.py",
    BACKEND / "app" / "f20_models.py",
    Path(__file__).resolve(),
}


def _python_files() -> list[Path]:
    return [
        path
        for path in [*(BACKEND / "app").rglob("*.py"), *(BACKEND / "tests").rglob("*.py")]
        if "__pycache__" not in path.parts
    ]


def _imports_legacy(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == LEGACY_MODULE:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == LEGACY_MODULE for alias in node.names):
                return True
    return False


def test_only_migrations_may_import_the_frozen_module_path() -> None:
    offenders = sorted(
        str(path.relative_to(BACKEND))
        for path in _python_files()
        if path not in ALLOWED and _imports_legacy(path)
    )
    assert offenders == [], (
        "these import the frozen path instead of app.registry_models: "
        f"{offenders}. The old name exists only so applied migrations keep "
        "their checksums; new code must not spread it."
    )


def test_migrations_still_import_the_frozen_path_unchanged() -> None:
    """The reason the shim exists, asserted so nobody 'tidies it up'.

    If someone repoints `migrations.py` at the new module, the checksums of
    every migration that imports models change, and every database on disk stops
    starting. That failure appears at the user's next launch, not in CI, which is
    why it is worth a test that looks wrong until you read this docstring.
    """
    source = (BACKEND / "app" / "migrations.py").read_text(encoding="utf-8")
    assert f"from {LEGACY_MODULE} import" in source, (
        "migrations.py no longer imports the frozen path — if this was "
        "deliberate, every existing database will refuse to start on upgrade"
    )


def test_the_shim_re_exports_the_models_migrations_actually_ask_for() -> None:
    import app.f20_models as shim
    import app.registry_models as current

    # The names appearing in migration bodies; a shim that dropped one would
    # fail at upgrade time on a real database and nowhere else.
    for name in (
        "F20Model",
        "EventSource",
        "EventSourceRevision",
        "SourceThanosConfig",
        "IncidentOccurrence",
        "MetricTemplateExtras",
        "ModelCallLog",
    ):
        assert getattr(shim, name) is getattr(current, name)


def test_the_base_class_name_is_left_alone_on_purpose() -> None:
    """`F20Model` is wrong and unfixable — see `registry_models.py`'s docstring.

    It appears in the source text of every model a migration lists in
    `checksum_dependencies`, so renaming it changes those checksums. This test
    exists so the next person to notice the bad name finds the reason instead of
    the bug.
    """
    from app.migrations import MIGRATIONS
    import inspect

    frozen = [
        dependency
        for migration in MIGRATIONS
        for dependency in migration.checksum_dependencies
    ]
    assert frozen, "no migration declares checksum dependencies any more"
    assert any(
        "F20Model" in inspect.getsource(dependency) for dependency in frozen
    ), "F20Model no longer appears in any frozen source — the rename may now be safe"
