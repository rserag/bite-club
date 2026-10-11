"""Optional reported preparation alongside immutable meal and favorite item snapshots."""

import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import metadata
from nutrition_bot.adapters.database.schema_favorites import favorite_items as favorite_items


def _details_table(name: str, parent: str, revision: str) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(revision, sa.Integer, primary_key=True),
        sa.Column("item_index", sa.Integer, primary_key=True),
        sa.Column("details", sa.JSON, nullable=False),
        sa.ForeignKeyConstraint(
            [revision, "item_index"],
            [f"{parent}.{revision}", f"{parent}.item_index"],
            ondelete="CASCADE",
        ),
    )


meal_preparation_details = _details_table("meal_preparation_details", "meal_items", "revision_id")
favorite_preparation_details = _details_table(
    "favorite_preparation_details", "favorite_items", "version_id"
)
