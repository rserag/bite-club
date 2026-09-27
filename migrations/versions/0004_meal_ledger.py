"""Whole-meal immutable revisions and consumed nutrient snapshots. Frozen migration."""

import sqlalchemy as sa
from alembic import op

revision = "0004_meal_ledger"
down_revision = "0003_food_sources"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
# Foreign-key targets only; existing tables are never recreated by this migration.
sa.Table("actions", metadata, sa.Column("key", sa.String, primary_key=True))
sa.Table("food_versions", metadata, sa.Column("id", sa.Integer, primary_key=True))
sa.Table("nutrients", metadata, sa.Column("code", sa.String, primary_key=True))

meals = sa.Table(
    "meals",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "current_revision_id",
        sa.Integer,
        sa.ForeignKey(
            "meal_revisions.id",
            name="meal_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    ),
    sa.Column("source_chat_id", sa.BigInteger, nullable=False),
    sa.Column("source_message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("source_chat_id", "source_message_id", name="meal_source_message"),
    sqlite_autoincrement=True,
)
meal_revisions = sa.Table(
    "meal_revisions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("meal_id", sa.Integer, sa.ForeignKey("meals.id", ondelete="CASCADE"), nullable=False),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column(
        "previous_revision_id",
        sa.Integer,
        sa.ForeignKey("meal_revisions.id", deferrable=True, initially="DEFERRED"),
    ),
    sa.Column(
        "action_key",
        sa.String,
        sa.ForeignKey("actions.key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("label", sa.String, nullable=False),
    sa.Column("local_date", sa.Date, nullable=False),
    sa.Column("timezone", sa.String, nullable=False),
    sa.Column("consumed_at", sa.Float, nullable=False),
    sa.Column("deleted", sa.Boolean, nullable=False),
    sa.Column("operation", sa.String, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False, server_default="0"),
    sa.UniqueConstraint("meal_id", "revision_number", name="meal_revision_number"),
    sa.CheckConstraint("revision_number > 0", name="meal_revision_positive"),
    sa.CheckConstraint("deleted IN (0,1) AND sealed IN (0,1)", name="meal_revision_booleans"),
    sa.CheckConstraint("operation IN ('create','edit','delete','undo')", name="meal_operation"),
    sa.CheckConstraint("length(label) BETWEEN 1 AND 120", name="meal_label_length"),
    sa.Index("ix_meal_revision_date", "local_date"),
    sqlite_autoincrement=True,
)
meal_items = sa.Table(
    "meal_items",
    metadata,
    sa.Column(
        "revision_id",
        sa.Integer,
        sa.ForeignKey("meal_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("item_index", sa.Integer, primary_key=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("food_name", sa.String, nullable=False),
    sa.Column("preparation", sa.String, nullable=False),
    sa.Column("source_kind", sa.String, nullable=False),
    sa.Column("source_reference", sa.String, nullable=False),
    sa.Column("source_url", sa.String),
    sa.Column("source_license", sa.String, nullable=False),
    sa.Column("food_content_sha256", sa.String, nullable=False),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("provenance", sa.JSON),
    sa.Column("edible_milligrams", sa.BigInteger, nullable=False),
    sa.Column("original_quantity", sa.String, nullable=False),
    sa.Column("original_unit", sa.String, nullable=False),
    sa.Column("quantity_method", sa.String, nullable=False),
    sa.CheckConstraint("item_index BETWEEN 0 AND 9", name="meal_item_index"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams BETWEEN 1 AND 50000000",
        name="meal_item_mass",
    ),
    sa.CheckConstraint("quantity_method = 'measured'", name="meal_quantity_method"),
    sa.CheckConstraint(
        "preparation IN ('raw','cooked','as_sold','as_prepared')", name="meal_food_preparation"
    ),
)
meal_item_nutrients = sa.Table(
    "meal_item_nutrients",
    metadata,
    sa.Column("revision_id", sa.Integer, primary_key=True),
    sa.Column("item_index", sa.Integer, primary_key=True),
    sa.Column(
        "nutrient_code",
        sa.String,
        sa.ForeignKey("nutrients.code", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("quality", sa.String, nullable=False),
    sa.ForeignKeyConstraint(
        ["revision_id", "item_index"],
        ["meal_items.revision_id", "meal_items.item_index"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("unit IN ('kcal','g','mg','ug')", name="meal_nutrient_unit"),
    sa.CheckConstraint(
        "amount_scaled IS NULL OR (typeof(amount_scaled) = 'integer' AND amount_scaled >= 0)",
        name="meal_nutrient_amount",
    ),
)


def upgrade():
    for table in (meals, meal_revisions, meal_items, meal_item_nutrients):
        table.create(op.get_bind())
    # Prevent SQLite's delete-then-insert REPLACE from mutating any existing identity.
    for table, condition in (
        (
            "meals",
            "id = NEW.id OR (source_chat_id = NEW.source_chat_id "
            "AND source_message_id = NEW.source_message_id)",
        ),
        (
            "meal_revisions",
            "id = NEW.id OR action_key = NEW.action_key OR "
            "(meal_id = NEW.meal_id AND revision_number = NEW.revision_number)",
        ),
        (
            "meal_items",
            "rowid = NEW.rowid OR (revision_id = NEW.revision_id AND item_index = NEW.item_index)",
        ),
        (
            "meal_item_nutrients",
            "rowid = NEW.rowid OR (revision_id = NEW.revision_id "
            "AND item_index = NEW.item_index AND nutrient_code = NEW.nutrient_code)",
        ),
    ):
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT, 'immutable meal identity'); END""")
    op.execute("""CREATE TRIGGER meal_identity_immutable BEFORE UPDATE ON meals
        WHEN OLD.id IS NOT NEW.id OR OLD.source_chat_id IS NOT NEW.source_chat_id
          OR OLD.source_message_id IS NOT NEW.source_message_id
          OR OLD.created_at IS NOT NEW.created_at
        BEGIN SELECT RAISE(ABORT, 'immutable meal identity'); END""")
    op.execute("""CREATE TRIGGER meal_current_revision_valid BEFORE UPDATE ON meals
        WHEN NOT EXISTS (SELECT 1 FROM meal_revisions WHERE id = NEW.current_revision_id
            AND meal_id = OLD.id AND sealed = 1
            AND previous_revision_id IS OLD.current_revision_id)
        BEGIN SELECT RAISE(ABORT, 'invalid meal revision transition'); END""")
    op.execute("""CREATE TRIGGER meal_revision_start_unsealed BEFORE INSERT ON meal_revisions
        WHEN NEW.sealed != 0
        BEGIN SELECT RAISE(ABORT, 'meal revision must be assembled before sealing'); END""")
    op.execute("""CREATE TRIGGER meal_revision_immutable BEFORE UPDATE ON meal_revisions
        WHEN OLD.sealed = 1
        BEGIN SELECT RAISE(ABORT, 'immutable meal revision'); END""")
    op.execute("""CREATE TRIGGER meal_revision_valid_seal BEFORE UPDATE ON meal_revisions
        WHEN NEW.sealed = 1 AND (
            (SELECT count(*) FROM meal_items WHERE revision_id = NEW.id) NOT BETWEEN 1 AND 10
            OR NOT EXISTS (SELECT 1 FROM meals WHERE id = NEW.meal_id
                AND current_revision_id IS NEW.previous_revision_id)
            OR (NEW.previous_revision_id IS NULL AND NEW.revision_number != 1)
            OR (NEW.previous_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM meal_revisions WHERE id = NEW.previous_revision_id
                AND meal_id = NEW.meal_id AND sealed = 1
                AND revision_number = NEW.revision_number - 1)))
        BEGIN SELECT RAISE(ABORT, 'incomplete meal revision'); END""")
    # Delete a complete meal to erase history; individual historic revisions cannot be removed.
    op.execute("""CREATE TRIGGER meal_revision_delete_history BEFORE DELETE ON meal_revisions
        WHEN EXISTS (SELECT 1 FROM meals WHERE id = OLD.meal_id)
        BEGIN SELECT RAISE(ABORT, 'erase complete meal history'); END""")
    for table in ("meal_items", "meal_item_nutrients"):
        for operation, refs in (
            ("insert", ("NEW",)),
            ("update", ("OLD", "NEW")),
            ("delete", ("OLD",)),
        ):
            condition = " OR ".join(
                f"EXISTS (SELECT 1 FROM meal_revisions WHERE id = {ref}.revision_id AND sealed = 1)"
                for ref in refs
            )
            op.execute(f"""CREATE TRIGGER {table}_{operation}_sealed
                BEFORE {operation.upper()} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'immutable meal snapshot'); END""")
    op.execute("""CREATE TRIGGER meal_nutrient_unit_valid BEFORE INSERT ON meal_item_nutrients
        WHEN NOT EXISTS (SELECT 1 FROM nutrients WHERE code = NEW.nutrient_code
                         AND unit = NEW.unit)
        BEGIN SELECT RAISE(ABORT, 'inconsistent nutrient unit'); END""")


def downgrade():
    # Drop guards first so an intentional downgrade can remove all ledger tables.
    for table in ("meal_items", "meal_item_nutrients"):
        for operation in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER {table}_{operation}_sealed")
    for name in (
        "meals_no_replace",
        "meal_revisions_no_replace",
        "meal_items_no_replace",
        "meal_item_nutrients_no_replace",
        "meal_identity_immutable",
        "meal_current_revision_valid",
        "meal_revision_start_unsealed",
        "meal_revision_immutable",
        "meal_revision_valid_seal",
        "meal_revision_delete_history",
        "meal_nutrient_unit_valid",
    ):
        op.execute(f"DROP TRIGGER {name}")
    for table in (meal_item_nutrients, meal_items, meals, meal_revisions):
        table.drop(op.get_bind())
