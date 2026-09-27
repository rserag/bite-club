"""Immutable training allocation proposals and approval history."""

from alembic import op

from nutrition_bot.adapters.database.schema_allocations import (
    allocation_events,
    allocation_proposals,
)

revision = "0021_training_allocations"
down_revision = "0020_supplement_plans"
branch_labels = None
depends_on = None
TABLES = (allocation_proposals, allocation_events)


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind())
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table.name}_{operation.lower()} BEFORE {operation} "
                f"ON {table.name} BEGIN SELECT RAISE(ABORT, 'immutable allocation history'); END"
            )


def downgrade() -> None:
    if any(
        op.get_bind().exec_driver_sql(f"SELECT count(*) FROM {t.name}").scalar_one() for t in TABLES
    ):
        raise RuntimeError("Allocation history exists; restore a matching backup.")
    for table in reversed(TABLES):
        op.drop_table(table.name)
