import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

daily_checkin_events = sa.Table(
    "daily_checkin_events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("food_status", sa.String, nullable=False),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "food_status IN ('complete','incomplete')", name="daily_checkin_food_status"
    ),
    sa.Index("ix_daily_checkin_date", "local_date", "id"),
    sqlite_autoincrement=True,
)
