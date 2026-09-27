"""Immutable versioned fixed-portion favorites. Frozen migration."""

import sqlalchemy as sa
from alembic import op

revision = "0008_favorites"
down_revision = "0007_food_aliases"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
sa.Table("actions", metadata, sa.Column("key", sa.String, primary_key=True))
sa.Table("food_versions", metadata, sa.Column("id", sa.Integer, primary_key=True))

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


def upgrade() -> None:
    bind = op.get_bind()
    for table in (favorites, favorite_versions, favorite_items):
        table.create(bind)
    for table, condition in (
        ("favorites", "id = NEW.id OR normalized_name = NEW.normalized_name"),
        (
            "favorite_versions",
            "id = NEW.id OR action_key = NEW.action_key OR "
            "(favorite_id = NEW.favorite_id AND version_number = NEW.version_number)",
        ),
        (
            "favorite_items",
            "rowid = NEW.rowid OR (version_id = NEW.version_id AND item_index = NEW.item_index)",
        ),
    ):
        op.execute(f"""CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {condition})
            BEGIN SELECT RAISE(ABORT, 'immutable favorite identity'); END""")
    op.execute("""CREATE TRIGGER favorite_identity_immutable BEFORE UPDATE ON favorites
        WHEN NEW.id IS NOT OLD.id OR NEW.name IS NOT OLD.name
            OR NEW.normalized_name IS NOT OLD.normalized_name
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'immutable favorite identity'); END""")
    op.execute("""CREATE TRIGGER favorite_current_version_valid
        BEFORE UPDATE OF current_version_id ON favorites
        WHEN NOT EXISTS (SELECT 1 FROM favorite_versions WHERE id = NEW.current_version_id
            AND favorite_id = OLD.id AND sealed = 1
            AND previous_version_id IS OLD.current_version_id)
        BEGIN SELECT RAISE(ABORT, 'invalid favorite version transition'); END""")
    op.execute("""CREATE TRIGGER favorite_start_without_version BEFORE INSERT ON favorites
        WHEN NEW.current_version_id IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'favorite must be assembled before selecting version'); END""")
    op.execute("""CREATE TRIGGER favorite_version_start_unsealed BEFORE INSERT ON favorite_versions
        WHEN NEW.sealed != 0
        BEGIN SELECT RAISE(ABORT, 'favorite version must be assembled before sealing'); END""")
    op.execute("""CREATE TRIGGER favorite_version_immutable BEFORE UPDATE ON favorite_versions
        WHEN OLD.sealed = 1
        BEGIN SELECT RAISE(ABORT, 'immutable favorite version'); END""")
    op.execute("""CREATE TRIGGER favorite_version_valid_seal BEFORE UPDATE ON favorite_versions
        WHEN NEW.sealed = 1 AND (
            (SELECT count(*) FROM favorite_items WHERE version_id = NEW.id) NOT BETWEEN 1 AND 10
            OR (SELECT max(item_index) FROM favorite_items WHERE version_id = NEW.id) !=
                (SELECT count(*) - 1 FROM favorite_items WHERE version_id = NEW.id)
            OR NOT EXISTS (SELECT 1 FROM favorites WHERE id = NEW.favorite_id
                AND current_version_id IS NEW.previous_version_id)
            OR (NEW.previous_version_id IS NULL AND NEW.version_number != 1)
            OR (NEW.previous_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM favorite_versions WHERE id = NEW.previous_version_id
                AND favorite_id = NEW.favorite_id AND sealed = 1
                AND version_number = NEW.version_number - 1)))
        BEGIN SELECT RAISE(ABORT, 'incomplete favorite version'); END""")
    op.execute("""CREATE TRIGGER favorite_version_delete_history BEFORE DELETE ON favorite_versions
        WHEN EXISTS (SELECT 1 FROM favorites WHERE id = OLD.favorite_id)
        BEGIN SELECT RAISE(ABORT, 'erase complete favorite history'); END""")
    for operation, refs in (
        ("insert", ("NEW",)),
        ("update", ("OLD", "NEW")),
        ("delete", ("OLD",)),
    ):
        condition = " OR ".join(
            f"EXISTS (SELECT 1 FROM favorite_versions WHERE id = {ref}.version_id AND sealed = 1)"
            for ref in refs
        )
        op.execute(f"""CREATE TRIGGER favorite_items_{operation}_sealed
            BEFORE {operation.upper()} ON favorite_items WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'immutable favorite portions'); END""")
    for operation in ("insert", "update"):
        op.execute(f"""CREATE TRIGGER favorite_item_reviewed_source_{operation}
            BEFORE {operation.upper()} ON favorite_items
            WHEN NOT EXISTS (SELECT 1 FROM food_versions
                WHERE id = NEW.food_version_id AND sealed = 1)
            BEGIN SELECT RAISE(ABORT, 'favorite requires a reviewed food version'); END""")


def downgrade() -> None:
    for name in (
        "favorites_no_replace",
        "favorite_versions_no_replace",
        "favorite_items_no_replace",
        "favorite_identity_immutable",
        "favorite_current_version_valid",
        "favorite_start_without_version",
        "favorite_version_start_unsealed",
        "favorite_version_immutable",
        "favorite_version_valid_seal",
        "favorite_version_delete_history",
        "favorite_items_insert_sealed",
        "favorite_items_update_sealed",
        "favorite_items_delete_sealed",
        "favorite_item_reviewed_source_insert",
        "favorite_item_reviewed_source_update",
    ):
        op.execute(f"DROP TRIGGER {name}")
    for table in (favorite_items, favorites, favorite_versions):
        table.drop(op.get_bind())
