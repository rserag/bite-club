"""Private BJJ templates, actual details and explicit training plans."""

from alembic import op

from nutrition_bot.adapters.database.schema_training import (
    bjj_session_details,
    bjj_template_revisions,
    training_plan_events,
    training_schedule_revisions,
    training_schedule_rules,
)

revision = "0017_bjj_plans_details"
down_revision = "0016_gym_details"
branch_labels = None
depends_on = None


TABLES = (
    "bjj_session_details",
    "bjj_template_revisions",
    "training_schedule_revisions",
    "training_schedule_rules",
    "training_plan_events",
)


def _guard(table: str) -> None:
    op.execute(
        f"""CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, 'immutable BJJ/plan history'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, 'immutable BJJ/plan history'); END"""
    )


def upgrade() -> None:
    bind = op.get_bind()
    bjj_session_details.create(bind)
    bjj_template_revisions.create(bind)
    training_schedule_revisions.create(bind)
    training_schedule_rules.create(bind)
    training_plan_events.create(bind)
    for table in TABLES:
        _guard(table)


def downgrade() -> None:
    bind = op.get_bind()
    if any(bind.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() for table in TABLES):
        raise RuntimeError(
            "BJJ template/detail or plan history exists; restore a matching backup "
            "instead of discarding it."
        )
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_update")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable_delete")
        op.drop_table(table)
