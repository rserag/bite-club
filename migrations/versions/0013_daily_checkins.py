"""Auditable food-day completeness events."""

from alembic import op

from nutrition_bot.adapters.database.schema_checkins import daily_checkin_events

revision = "0013_daily_checkins"
down_revision = "0012_body_weights"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    daily_checkin_events.create(bind)
    op.execute(
        """CREATE TRIGGER daily_checkin_events_immutable_update
        BEFORE UPDATE ON daily_checkin_events
        BEGIN SELECT RAISE(ABORT, 'immutable daily check-in event'); END"""
    )
    op.execute(
        """CREATE TRIGGER daily_checkin_events_immutable_delete
        BEFORE DELETE ON daily_checkin_events
        BEGIN SELECT RAISE(ABORT, 'immutable daily check-in event'); END"""
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT count(*) FROM daily_checkin_events").scalar_one():
        raise RuntimeError(
            "Daily completeness history exists; restore a matching backup instead of discarding it."
        )
    daily_checkin_events.drop(bind)
