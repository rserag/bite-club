"""Durable, bounded food-provider work; never contains consumed-food evidence."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

food_lookup_jobs = sa.Table(
    "food_lookup_jobs",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False),
    sa.Column("owner_id", sa.BigInteger, nullable=False),
    sa.Column("chat_id", sa.BigInteger, nullable=False),
    sa.Column("flow_revision", sa.Integer, nullable=False),
    sa.Column("kind", sa.String, nullable=False),
    sa.Column("request", sa.JSON, nullable=True),
    sa.Column("status", sa.String, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.Float),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("completed_at", sa.Float),
    sa.CheckConstraint("kind IN ('search','preview')", name="food_lookup_kind"),
    sa.CheckConstraint(
        "status IN ('pending','running','done','cancelled')", name="food_lookup_status"
    ),
    sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="food_lookup_attempts"),
    sa.Index("ix_food_lookup_pending", "status", "created_at"),
)
