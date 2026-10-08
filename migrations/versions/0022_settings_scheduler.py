"""Private editable preferences and durable, opt-in notification jobs."""

from alembic import op

from nutrition_bot.adapters.database.schema_settings import (
    notification_settings,
    reminder_deliveries,
    reminder_jobs,
    schedule_rules,
    settings_events,
)

revision = "0022_settings_scheduler"
down_revision = "0021_training_allocations"
branch_labels = None
depends_on = None
TABLES = (
    notification_settings,
    schedule_rules,
    settings_events,
    reminder_jobs,
    reminder_deliveries,
)


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind())
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER settings_events_{operation.lower()} BEFORE {operation} "
            "ON settings_events BEGIN SELECT RAISE(ABORT, 'immutable settings history'); END"
        )


def downgrade() -> None:
    if any(
        op.get_bind().exec_driver_sql(f"SELECT count(*) FROM {table.name}").scalar_one()
        for table in TABLES
    ):
        raise RuntimeError("Settings or reminder history exists; restore a matching backup.")
    for table in reversed(TABLES):
        op.drop_table(table.name)
