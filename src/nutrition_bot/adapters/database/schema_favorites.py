"""Versioned fixed-portion favorites in shared application metadata."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

favorites = sa.Table(
    "favorites",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("normalized_name", sa.String, nullable=False, unique=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column(
        "current_version_id",
        sa.Integer,
        sa.ForeignKey(
            "favorite_versions.id",
            name="favorite_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.CheckConstraint("length(name) BETWEEN 1 AND 60", name="favorite_name_length"),
    sa.CheckConstraint("length(normalized_name) BETWEEN 1 AND 60", name="favorite_key_length"),
    sqlite_autoincrement=True,
)

favorite_versions = sa.Table(
    "favorite_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "favorite_id", sa.Integer, sa.ForeignKey("favorites.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_version_id",
        sa.Integer,
        sa.ForeignKey("favorite_versions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("archived", sa.Boolean, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("favorite_id", "version_number", name="favorite_version_number"),
    sa.CheckConstraint("version_number > 0", name="favorite_version_positive"),
    sa.CheckConstraint("archived IN (0,1) AND sealed IN (0,1)", name="favorite_version_booleans"),
    sqlite_autoincrement=True,
)

favorite_items = sa.Table(
    "favorite_items",
    metadata,
    sa.Column(
        "version_id",
        sa.Integer,
        sa.ForeignKey("favorite_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("item_index", sa.Integer, primary_key=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("edible_milligrams", sa.BigInteger, nullable=False),
    sa.Column("estimate_basis", sa.String),
    sa.Column("recipe_share", sa.JSON(none_as_null=True)),
    sa.Column("recipe_version_id", sa.Integer, sa.ForeignKey("recipe_versions.id")),
    sa.CheckConstraint(
        "(recipe_share IS NULL) = (recipe_version_id IS NULL)", name="favorite_recipe_presence"
    ),
    sa.CheckConstraint("item_index BETWEEN 0 AND 9", name="favorite_item_index"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams BETWEEN 1 AND 50000000",
        name="favorite_item_mass",
    ),
    sa.CheckConstraint(
        "estimate_basis IS NULL OR length(trim(estimate_basis)) BETWEEN 1 AND 300",
        name="favorite_estimate_basis",
    ),
)
