"""Preserve explicit estimate approvals in immutable meal snapshots. Frozen migration."""

import sqlalchemy as sa
from alembic import op

revision = "0005_approved_estimates"
down_revision = "0004_meal_ledger"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
sa.Table("actions", metadata, sa.Column("key", sa.String, primary_key=True))
sa.Table("meal_revisions", metadata, sa.Column("id", sa.Integer, primary_key=True))
sa.Table("food_versions", metadata, sa.Column("id", sa.Integer, primary_key=True))
sa.Table("nutrients", metadata, sa.Column("code", sa.String, primary_key=True))

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
    sa.Column("quantity_basis", sa.String),
    sa.Column("approval_action_key", sa.String, sa.ForeignKey("actions.key", ondelete="RESTRICT")),
    sa.Column("approved_at", sa.Float),
    sa.Column("approval_draft_id", sa.BigInteger),
    sa.Column("approval_draft_revision", sa.BigInteger),
    sa.CheckConstraint("item_index BETWEEN 0 AND 9", name="meal_item_index"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams BETWEEN 1 AND 50000000",
        name="meal_item_mass",
    ),
    sa.CheckConstraint(
        "(quantity_method = 'measured' AND quantity_basis IS NULL "
        "AND approval_action_key IS NULL AND approved_at IS NULL "
        "AND approval_draft_id IS NULL AND approval_draft_revision IS NULL) OR "
        "(quantity_method = 'approved_estimate' AND quantity_basis IS NOT NULL "
        "AND length(trim(quantity_basis)) >= 1 AND length(quantity_basis) <= 300 "
        "AND approval_action_key IS NOT NULL AND substr(approval_action_key,1,9) = 'callback:' "
        "AND length(approval_action_key) > 9 AND approved_at IS NOT NULL "
        "AND approved_at > 0 AND approved_at <= 1.7976931348623157e308 "
        "AND typeof(approval_draft_id) = 'integer' AND approval_draft_id >= 1 "
        "AND typeof(approval_draft_revision) = 'integer' AND approval_draft_revision >= 1)",
        name="meal_quantity_method",
    ),
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


APPROVAL_COLUMNS = {
    "quantity_basis",
    "approval_action_key",
    "approved_at",
    "approval_draft_id",
    "approval_draft_revision",
}


def _rebuild(*, with_approvals):
    connection = op.get_bind()
    # Preserve guards verbatim, including guards added independently in later migrations.
    guards = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
        "AND tbl_name IN ('meal_items','meal_item_nutrients') ORDER BY name"
    ).all()
    # INSERT starts a real SQLite transaction before any destructive DDL. Unlike a batch
    # rename, rebuilding both dependent tables also works with foreign_keys left ON.
    for name in ("meal_items", "meal_item_nutrients"):
        connection.exec_driver_sql(
            f"CREATE TEMP TABLE estimate_backup_{name} AS SELECT * FROM {name} WHERE 0"
        )
        connection.exec_driver_sql(f"INSERT INTO estimate_backup_{name} SELECT * FROM {name}")
    for name, _ in guards:
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    connection.exec_driver_sql("DROP TABLE meal_item_nutrients")
    connection.exec_driver_sql("DROP TABLE meal_items")
    chosen_metadata = sa.MetaData()
    for name in ("actions", "meal_revisions", "food_versions", "nutrients"):
        metadata.tables[name].to_metadata(chosen_metadata)
    items = meal_items.to_metadata(chosen_metadata)
    nutrient_rows = meal_item_nutrients.to_metadata(chosen_metadata)
    if not with_approvals:
        for constraint in tuple(items.constraints):
            if constraint.name == "meal_quantity_method" or (
                isinstance(constraint, sa.ForeignKeyConstraint)
                and any(column.name == "approval_action_key" for column in constraint.columns)
            ):
                items.constraints.remove(constraint)
        for name in APPROVAL_COLUMNS:
            items._columns.remove(items.c[name])
        items.append_constraint(
            sa.CheckConstraint("quantity_method = 'measured'", name="meal_quantity_method")
        )
    for table in (items, nutrient_rows):
        table.create(connection)
        # Old rows receive NULL approval metadata; snapshots and row order remain intact.
        columns = ",".join(
            column.name for column in table.columns if column.name not in APPROVAL_COLUMNS
        )
        connection.exec_driver_sql(
            f"INSERT INTO {table.name} ({columns}) "
            f"SELECT {columns} FROM estimate_backup_{table.name}"
        )
    for _, sql in guards:
        connection.exec_driver_sql(sql)
    for name in ("meal_item_nutrients", "meal_items"):
        connection.exec_driver_sql(f"DROP TABLE estimate_backup_{name}")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("Meal estimate migration failed foreign-key validation")


def upgrade():
    _rebuild(with_approvals=True)


def downgrade():
    if (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM meal_items WHERE quantity_method != 'measured' LIMIT 1")
        .first()
        is not None
    ):
        raise RuntimeError("Cannot downgrade while estimate approval history exists")
    _rebuild(with_approvals=False)
