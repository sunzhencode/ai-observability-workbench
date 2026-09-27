"""Collapse the operator-controlled response lifecycle to three states.

Revision ID: platform_0013
Revises: platform_0012
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "platform_0013"
down_revision: str | None = "platform_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_STATES = (
    "'UNACKNOWLEDGED','ACKNOWLEDGED','INVESTIGATING','MITIGATING',"
    "'MONITORING','IN_PROGRESS','RESOLVED'"
)
_CURRENT_STATES = "'UNACKNOWLEDGED','IN_PROGRESS','RESOLVED'"
_LEGACY_STATES = (
    "'UNACKNOWLEDGED','ACKNOWLEDGED','INVESTIGATING','MITIGATING',"
    "'MONITORING','RESOLVED'"
)


def _replace_response_checks(
    *, before_suffix: str, after_suffix: str, allowed: str
) -> None:
    with op.batch_alter_table("operational_occurrence") as batch:
        batch.drop_constraint(
            f"ck_operational_occurrence_{before_suffix}", type_="check"
        )
        batch.create_check_constraint(
            f"ck_operational_occurrence_{after_suffix}",
            f"response_state IN ({allowed})",
        )
    with op.batch_alter_table("notification_route") as batch:
        batch.drop_constraint(
            f"ck_notification_route_{before_suffix}", type_="check"
        )
        batch.create_check_constraint(
            f"ck_notification_route_{after_suffix}",
            f"current_response_state IN ({allowed})",
        )


def upgrade() -> None:
    _replace_response_checks(
        before_suffix="response",
        after_suffix="response_transition",
        allowed=_ALL_STATES,
    )
    op.execute(
        """
        UPDATE operational_occurrence
        SET response_state = 'IN_PROGRESS'
        WHERE response_state IN ('ACKNOWLEDGED','INVESTIGATING','MITIGATING','MONITORING')
        """
    )
    op.execute(
        """
        UPDATE notification_route
        SET current_response_state = 'IN_PROGRESS', reminders_paused = 1
        WHERE current_response_state IN ('ACKNOWLEDGED','INVESTIGATING','MITIGATING','MONITORING')
        """
    )
    _replace_response_checks(
        before_suffix="response_transition",
        after_suffix="response",
        allowed=_CURRENT_STATES,
    )


def downgrade() -> None:
    _replace_response_checks(
        before_suffix="response",
        after_suffix="response_transition",
        allowed=_ALL_STATES,
    )
    op.execute(
        "UPDATE operational_occurrence SET response_state = 'ACKNOWLEDGED' "
        "WHERE response_state = 'IN_PROGRESS'"
    )
    op.execute(
        "UPDATE notification_route SET current_response_state = 'ACKNOWLEDGED' "
        "WHERE current_response_state = 'IN_PROGRESS'"
    )
    _replace_response_checks(
        before_suffix="response_transition",
        after_suffix="response",
        allowed=_LEGACY_STATES,
    )
