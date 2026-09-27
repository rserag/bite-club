"""Explicit personal food aliases registered in shared metadata."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

food_aliases = sa.Table(
    "food_aliases",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("normalized_name", sa.String, nullable=False, unique=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column(
        "created_action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "updated_action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.CheckConstraint("revision > 0 AND typeof(revision) = 'integer'", name="alias_revision"),
    sa.CheckConstraint("length(name) BETWEEN 1 AND 80", name="alias_name_length"),
    sa.CheckConstraint("length(normalized_name) BETWEEN 1 AND 240", name="alias_canonical_length"),
    sa.CheckConstraint("active IN (0,1)", name="alias_active"),
    sqlite_autoincrement=True,
)
