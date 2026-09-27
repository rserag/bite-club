"""Versioned batch recipes in shared application metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0009_recipes"
down_revision = "0008_favorites"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
sa.Table("actions", metadata, sa.Column("key", sa.String, primary_key=True))
sa.Table("food_versions", metadata, sa.Column("id", sa.Integer, primary_key=True))

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


def upgrade() -> None:
    bind = op.get_bind()
    for table in (recipes, recipe_versions, recipe_ingredients):
        table.create(bind)
    for table, condition in (
        ("recipes", "id = NEW.id OR normalized_name = NEW.normalized_name"),
        (
            "recipe_versions",
            "id = NEW.id OR action_key = NEW.action_key OR "
            "(recipe_id = NEW.recipe_id AND version_number = NEW.version_number)",
        ),
        (
            "recipe_ingredients",
            "rowid = NEW.rowid OR (version_id = NEW.version_id AND item_index = NEW.item_index)",
        ),
    ):
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT, 'immutable recipe identity'); END""")
    op.execute("""CREATE TRIGGER recipe_identity_immutable BEFORE UPDATE ON recipes
        WHEN NEW.id IS NOT OLD.id OR NEW.name IS NOT OLD.name
            OR NEW.normalized_name IS NOT OLD.normalized_name
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'immutable recipe identity'); END""")
    op.execute("""CREATE TRIGGER recipe_current_version_valid
        BEFORE UPDATE OF current_version_id ON recipes
        WHEN NOT EXISTS (SELECT 1 FROM recipe_versions WHERE id = NEW.current_version_id
            AND recipe_id = OLD.id AND sealed = 1
            AND previous_version_id IS OLD.current_version_id)
        BEGIN SELECT RAISE(ABORT, 'invalid recipe version transition'); END""")
    op.execute("""CREATE TRIGGER recipe_start_without_version BEFORE INSERT ON recipes
        WHEN NEW.current_version_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'recipe must be assembled before selecting version'); END""")
    op.execute("""CREATE TRIGGER recipe_version_start_unsealed BEFORE INSERT ON recipe_versions
        WHEN NEW.sealed != 0
        BEGIN SELECT RAISE(ABORT, 'recipe version must be assembled before sealing'); END""")
    op.execute("""CREATE TRIGGER recipe_version_immutable BEFORE UPDATE ON recipe_versions
        WHEN OLD.sealed = 1
        BEGIN SELECT RAISE(ABORT, 'immutable recipe version'); END""")
    op.execute("""CREATE TRIGGER recipe_version_valid_seal BEFORE UPDATE ON recipe_versions
        WHEN NEW.sealed = 1 AND (
            (SELECT count(*) FROM recipe_ingredients WHERE version_id = NEW.id) NOT BETWEEN 1 AND 10
            OR (SELECT max(item_index) FROM recipe_ingredients WHERE version_id = NEW.id) !=
                (SELECT count(*) - 1 FROM recipe_ingredients WHERE version_id = NEW.id)
            OR NOT EXISTS (SELECT 1 FROM recipes WHERE id = NEW.recipe_id
                AND current_version_id IS NEW.previous_version_id)
            OR (NEW.previous_version_id IS NULL AND NEW.version_number != 1)
            OR (NEW.previous_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM recipe_versions WHERE id = NEW.previous_version_id
                AND recipe_id = NEW.recipe_id AND sealed = 1
                AND version_number = NEW.version_number - 1)))
        BEGIN SELECT RAISE(ABORT, 'incomplete recipe version'); END""")
    op.execute("""CREATE TRIGGER recipe_version_delete_history BEFORE DELETE ON recipe_versions
        WHEN EXISTS (SELECT 1 FROM recipes WHERE id = OLD.recipe_id)
        BEGIN SELECT RAISE(ABORT, 'erase complete recipe history'); END""")
    for operation, refs in (
        ("insert", ("NEW",)),
        ("update", ("OLD", "NEW")),
        ("delete", ("OLD",)),
    ):
        condition = " OR ".join(
            f"EXISTS (SELECT 1 FROM recipe_versions WHERE id = {ref}.version_id AND sealed = 1)"
            for ref in refs
        )
        op.execute(f"""CREATE TRIGGER recipe_ingredients_{operation}_sealed
            BEFORE {operation.upper()} ON recipe_ingredients WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'immutable recipe portions'); END""")
    for operation in ("insert", "update"):
        op.execute(f"""CREATE TRIGGER recipe_item_reviewed_source_{operation}
            BEFORE {operation.upper()} ON recipe_ingredients
            WHEN NOT EXISTS (SELECT 1 FROM food_versions
                WHERE id = NEW.food_version_id AND sealed = 1)
            BEGIN SELECT RAISE(ABORT, 'recipe requires a reviewed food version'); END""")


def downgrade() -> None:
    for name in (
        "recipes_no_replace",
        "recipe_versions_no_replace",
        "recipe_ingredients_no_replace",
        "recipe_identity_immutable",
        "recipe_current_version_valid",
        "recipe_start_without_version",
        "recipe_version_start_unsealed",
        "recipe_version_immutable",
        "recipe_version_valid_seal",
        "recipe_version_delete_history",
        "recipe_ingredients_insert_sealed",
        "recipe_ingredients_update_sealed",
        "recipe_ingredients_delete_sealed",
        "recipe_item_reviewed_source_insert",
        "recipe_item_reviewed_source_update",
    ):
        op.execute(f"DROP TRIGGER {name}")
    for table in (recipe_ingredients, recipes, recipe_versions):
        table.drop(op.get_bind())
