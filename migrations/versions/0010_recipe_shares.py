"""Exact recipe provenance in meal/favorite snapshots and temporary draft pins."""

import sqlalchemy as sa
from alembic import op

revision = "0010_recipe_shares"
down_revision = "0009_recipes"
branch_labels = None
depends_on = None
metadata = sa.MetaData()
sa.Table("meal_drafts", metadata, sa.Column("id", sa.Integer, primary_key=True))
sa.Table("recipe_versions", metadata, sa.Column("id", sa.Integer, primary_key=True))
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


def upgrade() -> None:
    connection = op.get_bind()
    # ADD COLUMN preserves every existing sealed-ledger guard and food snapshot.
    # SQLite allows nullable REFERENCES columns without rebuilding FK dependents.
    for table, prefix in (("meal_items", "meal"), ("favorite_items", "favorite")):
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN recipe_share JSON")
        connection.exec_driver_sql(
            f"ALTER TABLE {table} ADD COLUMN recipe_version_id INTEGER "
            "REFERENCES recipe_versions(id) "
            f"CONSTRAINT {prefix}_recipe_presence "
            "CHECK ((recipe_share IS NULL) = (recipe_version_id IS NULL))"
        )
    draft_recipe_refs.create(connection)


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("meal_items", "favorite_items"):
        if connection.exec_driver_sql(
            f"SELECT count(*) FROM {table} "
            "WHERE recipe_version_id IS NOT NULL OR recipe_share IS NOT NULL"
        ).scalar_one():
            raise RuntimeError(
                "Recipe provenance exists; restore a matching backup "
                "instead of discarding its meaning."
            )
    if connection.exec_driver_sql("SELECT count(*) FROM draft_recipe_refs").scalar_one():
        raise RuntimeError(
            "Recipe drafts exist; restore a matching backup instead of discarding their meaning."
        )
    draft_recipe_refs.drop(connection)
    for table in ("meal_items", "favorite_items"):
        connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN recipe_version_id")
        connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN recipe_share")
