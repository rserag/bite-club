"""Optional immutable recovery check-ins."""

from alembic import op

from nutrition_bot.adapters.database.schema_recovery import recovery_checkins, recovery_revisions

revision = "0018_recovery_checkins"
down_revision = "0017_bjj_plans_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    recovery_checkins.create(bind)
    recovery_revisions.create(bind)
    op.execute(
        """CREATE TRIGGER recovery_revisions_immutable_update
        BEFORE UPDATE ON recovery_revisions
        BEGIN SELECT RAISE(ABORT, 'immutable recovery history'); END"""
    )
    op.execute(
        """CREATE TRIGGER recovery_revisions_immutable_delete
        BEFORE DELETE ON recovery_revisions
        BEGIN SELECT RAISE(ABORT, 'immutable recovery history'); END"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT count(*) FROM recovery_revisions").scalar_one():
        raise RuntimeError(
            "Recovery history exists; restore a matching backup instead of discarding it."
        )
    recovery_revisions.drop(bind)
    recovery_checkins.drop(bind)
