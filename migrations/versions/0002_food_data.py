"""Immutable food snapshots and explicit nutrient units. Frozen migration."""

import sqlalchemy as sa
from alembic import op

revision = "0002_food_data"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
nutrients = sa.Table(
    "nutrients",
    metadata,
    sa.Column("code", sa.String, primary_key=True),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("unit", sa.String, nullable=False),
    sa.Column("definition", sa.String, nullable=False),
    sa.CheckConstraint("unit IN ('kcal','g','mg','ug')", name="nutrient_unit"),
)
foods = sa.Table(
    "foods",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("preparation", sa.String, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.CheckConstraint(
        "preparation IN ('raw','cooked','as_sold','as_prepared','unspecified')",
        name="food_preparation",
    ),
    sqlite_autoincrement=True,
)
food_versions = sa.Table(
    "food_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("food_id", sa.Integer, sa.ForeignKey("foods.id"), nullable=False),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.Column("name", sa.String, nullable=False),
    sa.Column("brand", sa.String),
    sa.Column("source_kind", sa.String, nullable=False),
    sa.Column("source_reference", sa.String, nullable=False),
    sa.Column("source_url", sa.String),
    sa.Column("source_license", sa.String, nullable=False),
    sa.Column("source_basis_milligrams", sa.BigInteger, nullable=False),
    sa.Column("reviewed_at", sa.Float, nullable=False),
    sa.Column("calculation_version", sa.String, nullable=False),
    sa.Column("content_sha256", sa.String, nullable=False),
    sa.Column("sealed", sa.Boolean, nullable=False, server_default="0"),
    sa.UniqueConstraint("food_id", "version_number", name="food_version_number"),
    sa.CheckConstraint("version_number > 0", name="food_version_positive"),
    sa.CheckConstraint("sealed IN (0,1)", name="food_version_sealed"),
    sa.CheckConstraint(
        "typeof(source_basis_milligrams) = 'integer' AND source_basis_milligrams > 0",
        name="food_source_basis_positive",
    ),
    sqlite_autoincrement=True,
)
food_nutrients = sa.Table(
    "food_nutrients",
    metadata,
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("nutrient_code", sa.String, sa.ForeignKey("nutrients.code"), primary_key=True),
    sa.Column("amount_scaled", sa.BigInteger),
    sa.Column("source_amount", sa.String),
    sa.Column("source_unit", sa.String, nullable=False),
    sa.Column("quality", sa.String, nullable=False),
    sa.Column("note", sa.String),
    sa.CheckConstraint(
        "amount_scaled IS NULL OR (typeof(amount_scaled) = 'integer' AND amount_scaled >= 0)",
        name="food_nutrient_nonnegative",
    ),
    sa.CheckConstraint(
        "(amount_scaled IS NULL) = (source_amount IS NULL)",
        name="food_nutrient_missingness",
    ),
    sa.CheckConstraint("source_unit IN ('kcal','g','mg','ug')", name="food_nutrient_source_unit"),
)
food_portions = sa.Table(
    "food_portions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "food_version_id",
        sa.Integer,
        sa.ForeignKey("food_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("label", sa.String, nullable=False),
    sa.Column("edible_milligrams", sa.BigInteger, nullable=False),
    sa.Column("original_measure", sa.String, nullable=False),
    sa.Column("source", sa.String, nullable=False),
    sa.Column("is_estimate", sa.Boolean, nullable=False),
    sa.UniqueConstraint("food_version_id", "label", name="food_portion_label"),
    sa.CheckConstraint(
        "typeof(edible_milligrams) = 'integer' AND edible_milligrams > 0",
        name="food_portion_positive",
    ),
    sa.CheckConstraint("is_estimate IN (0,1)", name="food_portion_estimate"),
)


# These define nutrient meaning, not reference targets or invented food values.
REGISTRY = (
    ("energy", "Energy", "kcal", "Source-reported metabolizable food energy"),
    ("protein", "Protein", "g", "Total protein"),
    ("carbohydrate", "Carbohydrate", "g", "Total carbohydrate including dietary fiber"),
    ("fat", "Fat", "g", "Total lipid"),
    ("fiber", "Fiber", "g", "Total dietary fiber"),
    ("sodium", "Sodium", "mg", "Elemental sodium, not salt mass"),
    ("potassium", "Potassium", "mg", "Elemental potassium"),
    ("calcium", "Calcium", "mg", "Elemental calcium"),
    ("magnesium", "Magnesium", "mg", "Elemental magnesium"),
    ("iron", "Iron", "mg", "Elemental iron"),
    ("zinc", "Zinc", "mg", "Elemental zinc"),
    ("vitamin_d", "Vitamin D", "ug", "Vitamin D2 plus D3 mass, not IU"),
    ("vitamin_b12", "Vitamin B12", "ug", "Total vitamin B12"),
    ("vitamin_c", "Vitamin C", "mg", "Total ascorbic acid"),
)


def upgrade():
    metadata.create_all(op.get_bind())
    op.bulk_insert(
        nutrients,
        [dict(zip(("code", "name", "unit", "definition"), row, strict=True)) for row in REGISTRY],
    )
    # Registry meaning and preparation cannot change beneath historical snapshots.
    for table in ("nutrients", "foods"):
        op.execute(f"""CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'immutable food definition'); END""")
    # REPLACE deletes then inserts and can bypass UPDATE/FK protections in SQLite.
    # Guard before deletion, including alternate version-uniqueness conflicts.
    for table, condition in (
        ("nutrients", "code = NEW.code OR rowid = NEW.rowid"),
        ("foods", "id = NEW.id"),
        (
            "food_versions",
            "id = NEW.id OR (food_id = NEW.food_id AND version_number = NEW.version_number)",
        ),
    ):
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT, 'immutable food identity'); END""")
    op.execute("""CREATE TRIGGER food_version_immutable BEFORE UPDATE ON food_versions
        WHEN OLD.sealed = 1
        BEGIN SELECT RAISE(ABORT, 'immutable food version'); END""")
    for table in ("food_nutrients", "food_portions"):
        # Explicit rowid replacement must not move an old sealed child into a draft.
        op.execute(f"""CREATE TRIGGER {table}_no_row_replace BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} AS child
                JOIN food_versions AS version ON version.id = child.food_version_id
                WHERE child.rowid = NEW.rowid AND version.sealed = 1)
            BEGIN SELECT RAISE(ABORT, 'immutable food snapshot'); END""")
        for operation, refs in (
            ("insert", ("NEW",)),
            ("update", ("OLD", "NEW")),
            ("delete", ("OLD",)),
        ):
            condition = " OR ".join(
                f"EXISTS (SELECT 1 FROM food_versions WHERE id = {ref}.food_version_id "
                "AND sealed = 1)"
                for ref in refs
            )
            op.execute(f"""CREATE TRIGGER {table}_{operation}_sealed
                BEFORE {operation.upper()} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'immutable food snapshot'); END""")


def downgrade():
    metadata.drop_all(op.get_bind())
