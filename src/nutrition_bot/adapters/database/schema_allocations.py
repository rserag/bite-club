import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

allocation_proposals = sa.Table(
    "allocation_proposals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("week_start", sa.Date, nullable=False),
    sa.Column("base_plan_id", sa.Integer, sa.ForeignKey("target_plans.id"), nullable=False),
    sa.Column("kinds", sa.JSON, nullable=False),
    sa.Column("deltas", sa.JSON, nullable=False),
    sa.Column("step_kcal", sa.Integer, nullable=False),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint("step_kcal BETWEEN 0 AND 300 AND step_kcal % 4 = 0", name="allocation_step"),
    sa.Index("ix_allocation_week", "week_start", "id"),
    sqlite_autoincrement=True,
)
allocation_events = sa.Table(
    "allocation_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("proposal_id", sa.Integer, sa.ForeignKey("allocation_proposals.id"), nullable=False),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint("kind IN ('apply','cancel')", name="allocation_event_kind"),
    sa.UniqueConstraint("proposal_id", "kind", name="allocation_once"),
    sqlite_autoincrement=True,
)
