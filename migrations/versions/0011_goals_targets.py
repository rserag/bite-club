"""Reviewed goals and immutable future-effective target plans."""

from alembic import op

from nutrition_bot.adapters.database.schema_goals import goal_proposals, goals, target_plans

revision = "0011_goals_targets"
down_revision = "0010_recipe_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in (goal_proposals, goals, target_plans):
        table.create(bind)
    op.execute(
        """CREATE TRIGGER target_plans_immutable_update BEFORE UPDATE ON target_plans
        BEGIN SELECT RAISE(ABORT, 'immutable target plan'); END"""
    )
    op.execute(
        """CREATE TRIGGER target_plans_immutable_delete BEFORE DELETE ON target_plans
        BEGIN SELECT RAISE(ABORT, 'immutable target plan'); END"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("target_plans", "goals", "goal_proposals"):
        if bind.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one():
            raise RuntimeError(
                "Goal or target history exists; restore a matching backup instead of discarding it."
            )
    for table in (target_plans, goals, goal_proposals):
        table.drop(bind)
