"""Versioned batch recipes in shared application metadata."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata

recipes = sa.Table(
    "recipes",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("normalized_name", sa.String, nullable=False, unique=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column(
        "current_version_id",
        sa.Integer,
        sa.ForeignKey(
            "recipe_versions.id",
            name="recipe_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.CheckConstraint("length(name) BETWEEN 1 AND 60", name="recipe_name_length"),
    sa.CheckConstraint("length(normalized_name) BETWEEN 1 AND 60", name="recipe_key_length"),
    sqlite_autoincrement=True,
)

recipe_versions = sa.Table(
    "recipe_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "recipe_id", sa.Integer, sa.ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_version_id",
        sa.Integer,
        sa.ForeignKey("recipe_versions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("total_units", sa.BigInteger, nullable=False),
    sa.Column("estimate_basis", sa.String),
    sa.CheckConstraint("unit IN ('g','serving')", name="recipe_unit"),
    sa.CheckConstraint(
        "typeof(total_units) = 'integer' AND total_units > 0 AND "
        "((unit = 'g' AND total_units <= 50000000) OR "
        "(unit = 'serving' AND total_units <= 1000000))",
        name="recipe_total_units",
    ),
    sa.CheckConstraint(
        "estimate_basis IS NULL OR length(trim(estimate_basis)) BETWEEN 1 AND 300",
        name="recipe_yield_basis",
    ),
    sa.Column("archived", sa.Boolean, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False, server_default="0"),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("recipe_id", "version_number", name="recipe_version_number"),
    sa.CheckConstraint("version_number > 0", name="recipe_version_positive"),
    sa.CheckConstraint("archived IN (0,1) AND sealed IN (0,1)", name="recipe_version_booleans"),
    sqlite_autoincrement=True,
)

recipe_ingredients = sa.Table(
    "recipe_ingredients",
    metadata,
    sa.Column(
        "version_id",
        sa.Integer,
        sa.ForeignKey("recipe_versions.id", ondelete="CASCADE"),
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
    sa.CheckConstraint("item_index BETWEEN 0 AND 9", name="recipe_item_index"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams BETWEEN 1 AND 50000000",
        name="recipe_item_mass",
    ),
    sa.CheckConstraint(
        "estimate_basis IS NULL OR length(trim(estimate_basis)) BETWEEN 1 AND 300",
        name="recipe_estimate_basis",
    ),
)


draft_recipe_refs = sa.Table(
    "draft_recipe_refs",
    metadata,
    sa.Column(
        "draft_id",
        sa.Integer,
        sa.ForeignKey("meal_drafts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "recipe_version_id",
        sa.Integer,
        sa.ForeignKey("recipe_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)
