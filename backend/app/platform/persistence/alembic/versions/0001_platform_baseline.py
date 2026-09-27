"""Establish the empty Incident Operations schema family without importing legacy metadata.

Revision ID: platform_0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "platform_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The baseline is intentionally empty; later revisions add platform tables."""


def downgrade() -> None:
    """Downgrading the empty baseline only clears Alembic's revision marker."""
