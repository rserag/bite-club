"""Auditable body-weight revisions."""

from alembic import op

from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights

revision = "0012_body_weights"
down_revision = "0011_goals_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    body_weights.create(bind)
    body_weight_revisions.create(bind)
    op.execute(
        """CREATE TRIGGER body_weight_revisions_immutable_update
        BEFORE UPDATE ON body_weight_revisions
        BEGIN SELECT RAISE(ABORT, 'immutable body-weight revision'); END"""
    )
    op.execute(
        """CREATE TRIGGER body_weight_revisions_immutable_delete
        BEFORE DELETE ON body_weight_revisions
        BEGIN SELECT RAISE(ABORT, 'immutable body-weight revision'); END"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT count(*) FROM body_weight_revisions").scalar_one():
        raise RuntimeError(
            "Body-weight history exists; restore a matching backup instead of discarding it."
        )
    body_weight_revisions.drop(bind)
    body_weights.drop(bind)
