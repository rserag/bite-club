"""Private AI attempt accounting; no prompts, raw responses or photo copies."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

ai_budget_periods = sa.Table(
    "ai_budget_periods",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("starts_at", sa.Float, nullable=False, unique=True),
    sa.Column("ends_at", sa.Float, nullable=False, unique=True),
    sa.Column("limit_micro_usd", sa.BigInteger, nullable=False),
    sa.CheckConstraint("ends_at > starts_at", name="ai_period_range"),
    sa.CheckConstraint("limit_micro_usd = 10000000", name="ai_base_limit"),
)

ai_requests = sa.Table(
    "ai_requests",
    metadata,
    sa.Column("request_key", sa.String, primary_key=True),
    sa.Column("role", sa.String, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("outcome", sa.JSON(none_as_null=True)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("completed_at", sa.Float),
    sa.CheckConstraint("role IN ('meal_text','meal_photo')", name="ai_request_role"),
    sa.CheckConstraint("state IN ('running','done','unknown')", name="ai_request_state"),
)

ai_attempts = sa.Table(
    "ai_attempts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("request_key", sa.String, sa.ForeignKey("ai_requests.request_key"), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("period", sa.String, sa.ForeignKey("ai_budget_periods.id"), nullable=False),
    sa.Column("route_id", sa.String, nullable=False),
    sa.Column("reserved_micro_usd", sa.BigInteger, nullable=False),
    sa.Column("charged_micro_usd", sa.BigInteger),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("generation_id", sa.String),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("completed_at", sa.Float),
    sa.UniqueConstraint("request_key", "attempt", name="ai_request_attempt"),
    sa.CheckConstraint("attempt BETWEEN 1 AND 2", name="ai_bounded_attempt"),
    sa.CheckConstraint("reserved_micro_usd > 0", name="ai_positive_reservation"),
    sa.CheckConstraint("charged_micro_usd >= 0", name="ai_nonnegative_charge"),
    sa.CheckConstraint("state IN ('reserved','settled','unknown')", name="ai_attempt_state"),
    sa.CheckConstraint(
        "(state = 'settled') = (charged_micro_usd IS NOT NULL)", name="ai_charge_state"
    ),
    sa.Index("ix_ai_budget_period", "period"),
    sqlite_autoincrement=True,
)

ai_disabled_routes = sa.Table(
    "ai_disabled_routes",
    metadata,
    sa.Column("route_id", sa.String, primary_key=True),
    sa.Column("reason", sa.String, nullable=False),
    sa.Column("disabled_at", sa.Float, nullable=False),
    sa.CheckConstraint("reason IN ('policy','pricing','usage')", name="ai_route_reason"),
)


ai_plan_invocations = sa.Table(
    "ai_plan_invocations",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "request_key",
        sa.String,
        sa.ForeignKey("ai_requests.request_key"),
        nullable=False,
        unique=True,
    ),
    sa.Column("day", sa.String, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("completed_at", sa.Float),
    sa.CheckConstraint("state IN ('started','done','unknown')", name="ai_plan_state"),
    sa.Index("ix_ai_plan_day", "day"),
    sqlite_autoincrement=True,
)
