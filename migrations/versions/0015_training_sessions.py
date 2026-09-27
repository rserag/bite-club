"""Immutable quick gym and BJJ session revisions."""

from alembic import op

from nutrition_bot.adapters.database.schema_training import (
    training_session_revisions,
    training_sessions,
)

revision = "0015_training_sessions"
down_revision = "0014_adaptive_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    training_sessions.create(bind)
    training_session_revisions.create(bind)
    op.execute(
        """CREATE TRIGGER training_session_revisions_immutable_update
        BEFORE UPDATE ON training_session_revisions
        BEGIN SELECT RAISE(ABORT, 'immutable training-session revision'); END"""
    )
    op.execute(
        """CREATE TRIGGER training_session_revisions_immutable_delete
        BEFORE DELETE ON training_session_revisions
        BEGIN SELECT RAISE(ABORT, 'immutable training-session revision'); END"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT count(*) FROM training_session_revisions").scalar_one():
        raise RuntimeError(
            "Training history exists; restore a matching backup instead of discarding it."
        )
    training_session_revisions.drop(bind)
    training_sessions.drop(bind)
