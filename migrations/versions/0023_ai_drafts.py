"""Restart-safe paid AI reservations and reviewed meal proposals."""

from alembic import op

from nutrition_bot.adapters.database.schema_ai import (
    ai_attempts,
    ai_budget_periods,
    ai_disabled_routes,
    ai_plan_invocations,
    ai_requests,
)

revision = "0023_ai_drafts"
down_revision = "0022_settings_scheduler"
branch_labels = None
depends_on = None
TABLES = (ai_budget_periods, ai_requests, ai_attempts, ai_disabled_routes, ai_plan_invocations)


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind())


def downgrade() -> None:
    if (
        op.get_bind().exec_driver_sql("SELECT count(*) FROM ai_attempts").scalar_one()
        or op.get_bind().exec_driver_sql("SELECT count(*) FROM ai_plan_invocations").scalar_one()
    ):
        raise RuntimeError("AI spending history exists; restore a matching backup.")
    for table in reversed(TABLES):
        op.drop_table(table.name)
