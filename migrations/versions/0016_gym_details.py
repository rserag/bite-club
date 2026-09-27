"""Immutable optional gym exercise and set snapshots."""

from alembic import op

from nutrition_bot.adapters.database.schema_training import (
    gym_exercise_occurrences,
    gym_workout_sets,
)

revision = "0016_gym_details"
down_revision = "0015_training_sessions"
branch_labels = None
depends_on = None


def _guard(table: str) -> None:
    op.execute(
        f"""CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, 'immutable gym-detail snapshot'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, 'immutable gym-detail snapshot'); END"""
    )


def upgrade() -> None:
    bind = op.get_bind()
    gym_exercise_occurrences.create(bind)
    gym_workout_sets.create(bind)
    _guard("gym_exercise_occurrences")
    _guard("gym_workout_sets")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT count(*) FROM gym_exercise_occurrences").scalar_one():
        raise RuntimeError(
            "Gym detail history exists; restore a matching backup instead of discarding it."
        )
    gym_workout_sets.drop(bind)
    gym_exercise_occurrences.drop(bind)
