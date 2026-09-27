"""Evidence-gated adaptive target reviews and proposals."""

import sqlalchemy as sa
from alembic import op

from nutrition_bot.adapters.database.schema_adaptive import adaptive_proposals, adaptive_reviews

revision = "0014_adaptive_targets"
down_revision = "0013_daily_checkins"
branch_labels = None
depends_on = None


def _target_triggers() -> None:
    op.execute(
        """CREATE TRIGGER target_plans_immutable_update BEFORE UPDATE ON target_plans
        BEGIN SELECT RAISE(ABORT, 'immutable target plan'); END"""
    )
    op.execute(
        """CREATE TRIGGER target_plans_immutable_delete BEFORE DELETE ON target_plans
        BEGIN SELECT RAISE(ABORT, 'immutable target plan'); END"""
    )


def _replace_target_schema(*, adaptive: bool) -> None:
    op.execute("DROP TRIGGER target_plans_immutable_update")
    op.execute("DROP TRIGGER target_plans_immutable_delete")
    with op.batch_alter_table("target_plans", recreate="always") as batch:
        batch.drop_constraint("target_plan_source", type_="check")
        if adaptive:
            batch.add_column(
                sa.Column("energy_range_low_kcal", sa.Integer()),
                insert_after="carbohydrate_grams",
            )
            batch.add_column(
                sa.Column("energy_range_high_kcal", sa.Integer()),
                insert_after="energy_range_low_kcal",
            )
            batch.create_check_constraint(
                "target_plan_energy_range",
                "(energy_range_low_kcal IS NULL AND energy_range_high_kcal IS NULL) OR "
                "(energy_range_low_kcal IS NOT NULL AND energy_range_high_kcal IS NOT NULL "
                "AND energy_range_low_kcal BETWEEN 1 AND energy_kcal "
                "AND energy_range_high_kcal BETWEEN energy_kcal AND 10000)",
            )
        else:
            batch.drop_constraint("target_plan_energy_range", type_="check")
            batch.drop_column("energy_range_low_kcal")
            batch.drop_column("energy_range_high_kcal")
        expression = (
            "source IN ('initial_estimate','manual','adaptive')"
            if adaptive
            else "source IN ('initial_estimate','manual')"
        )
        batch.create_check_constraint("target_plan_source", expression)
        batch.alter_column("proposal_id", existing_type=sa.Integer(), nullable=adaptive)
    if not adaptive:
        _target_triggers()


def upgrade() -> None:
    _replace_target_schema(adaptive=True)
    op.execute(
        """UPDATE target_plans
        SET energy_range_low_kcal = (
                SELECT energy_range_low_kcal FROM goal_proposals
                WHERE goal_proposals.id = target_plans.proposal_id
            ),
            energy_range_high_kcal = (
                SELECT energy_range_high_kcal FROM goal_proposals
                WHERE goal_proposals.id = target_plans.proposal_id
            )"""
    )
    _target_triggers()
    bind = op.get_bind()
    adaptive_reviews.create(bind)
    adaptive_proposals.create(bind)
    op.execute(
        """CREATE TRIGGER adaptive_reviews_immutable_update BEFORE UPDATE ON adaptive_reviews
        BEGIN SELECT RAISE(ABORT, 'immutable adaptive review'); END"""
    )
    op.execute(
        """CREATE TRIGGER adaptive_reviews_immutable_delete BEFORE DELETE ON adaptive_reviews
        BEGIN SELECT RAISE(ABORT, 'immutable adaptive review'); END"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("adaptive_proposals", "adaptive_reviews"):
        if bind.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one():
            raise RuntimeError(
                "Adaptive review history exists; restore a matching backup instead "
                "of discarding it."
            )
    adaptive_proposals.drop(bind)
    adaptive_reviews.drop(bind)
    _replace_target_schema(adaptive=False)
