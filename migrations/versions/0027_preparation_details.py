"""Preserve reported preparation without rewriting any existing source snapshot."""

from alembic import op

from nutrition_bot.adapters.database.schema_preparation import (
    favorite_preparation_details,
    meal_preparation_details,
)

revision = "0027_preparation_details"
down_revision = "0026_food_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, revision_table, key in (
        (meal_preparation_details, "meal_revisions", "revision_id"),
        (favorite_preparation_details, "favorite_versions", "version_id"),
    ):
        table.create(op.get_bind())
        op.execute(f"""CREATE TRIGGER {table.name}_no_replace BEFORE INSERT ON {table.name}
            WHEN EXISTS (SELECT 1 FROM {table.name}
                WHERE {key}=NEW.{key} AND item_index=NEW.item_index)
            BEGIN SELECT RAISE(ABORT, 'immutable preparation identity'); END""")
        for operation, refs in (
            ("insert", ("NEW",)),
            ("update", ("OLD", "NEW")),
            ("delete", ("OLD",)),
        ):
            condition = " OR ".join(
                f"EXISTS (SELECT 1 FROM {revision_table} WHERE id={ref}.{key} AND sealed=1)"
                for ref in refs
            )
            op.execute(f"""CREATE TRIGGER {table.name}_{operation}_sealed
                BEFORE {operation.upper()} ON {table.name} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'immutable preparation snapshot'); END""")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.exec_driver_sql(
        "SELECT 1 FROM meal_drafts, json_each(meal_drafts.content, '$.items') AS item "
        "WHERE json_type(item.value, '$.preparation_details') IS NOT NULL "
        "AND json_type(item.value, '$.preparation_details') != 'null' LIMIT 1"
    ).first():
        raise RuntimeError(
            "Preparation drafts exist; keep this schema and disable AI, "
            "or restore a matching backup instead of discarding their meaning."
        )
    for table in (meal_preparation_details, favorite_preparation_details):
        if connection.exec_driver_sql(f"SELECT count(*) FROM {table.name}").scalar_one():
            raise RuntimeError(
                "Reported preparation exists; keep this schema and disable AI, "
                "or restore a matching backup instead of discarding its meaning."
            )
    for table in (favorite_preparation_details, meal_preparation_details):
        for suffix in ("no_replace", "insert_sealed", "update_sealed", "delete_sealed"):
            op.execute(f"DROP TRIGGER {table.name}_{suffix}")
        table.drop(connection)
