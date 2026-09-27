"""Immutable plan proposals, resolutions and dose marks."""

from alembic import op

from nutrition_bot.adapters.database.schema_supplement_plans import (
    plan_dose_marks,
    plan_proposals,
    plan_resolutions,
)

revision = "0020_supplement_plans"
down_revision = "0019_supplement_foundation"
branch_labels = None
depends_on = None
TABLES = (plan_proposals, plan_resolutions, plan_dose_marks)


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind())
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table.name}_{operation.lower()} BEFORE {operation} "
                f"ON {table.name} BEGIN SELECT RAISE(ABORT, 'immutable plan history'); END"
            )


def downgrade() -> None:
    if any(
        op.get_bind().exec_driver_sql(f"SELECT count(*) FROM {t.name}").scalar_one() for t in TABLES
    ):
        raise RuntimeError("Supplement plan history exists; restore a matching backup.")
    for table in reversed(TABLES):
        op.drop_table(table.name)
