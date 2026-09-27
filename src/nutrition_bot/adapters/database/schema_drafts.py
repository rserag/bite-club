"""Temporary draft tables, registered in the shared application metadata."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

meal_drafts = sa.Table(
    "meal_drafts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("state", sa.String, nullable=False),
    sa.Column("content", sa.JSON(none_as_null=True)),
    sa.Column("source_chat_id", sa.BigInteger, nullable=False),
    sa.Column("source_message_id", sa.BigInteger, nullable=False),
    sa.Column(
        "created_action_key", sa.String, sa.ForeignKey("actions.key"), nullable=False, unique=True
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("last_user_activity_at", sa.Float, nullable=False),
    sa.Column("retention_purged_at", sa.Float),
    sa.Column("saved_meal_id", sa.Integer, sa.ForeignKey("meals.id", ondelete="SET NULL")),
    sa.UniqueConstraint("source_chat_id", "source_message_id", name="draft_source_message"),
    sa.CheckConstraint("revision > 0", name="draft_revision_positive"),
    sa.CheckConstraint("state IN ('open','saved','cancelled','expired')", name="draft_state"),
    sa.CheckConstraint("(state = 'open') = (content IS NOT NULL)", name="draft_content_state"),
    sa.CheckConstraint("state = 'saved' OR saved_meal_id IS NULL", name="draft_saved_meal"),
    sa.Index("ix_draft_activity", "last_user_activity_at"),
    sqlite_autoincrement=True,
)

draft_food_refs = sa.Table(
    "draft_food_refs",
    metadata,
    sa.Column(
        "draft_id",
        sa.Integer,
        sa.ForeignKey("meal_drafts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)

draft_action_links = sa.Table(
    "draft_action_links",
    metadata,
    sa.Column(
        "draft_id",
        sa.Integer,
        sa.ForeignKey("meal_drafts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "action_key", sa.String, sa.ForeignKey("actions.key", ondelete="CASCADE"), primary_key=True
    ),
)
