"""Read per-entry nutrient provenance from current immutable meal snapshots."""

from datetime import date

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import meal_item_nutrients as amounts
from nutrition_bot.adapters.database.schema import meal_items as items
from nutrition_bot.adapters.database.schema import meal_revisions as revisions
from nutrition_bot.adapters.database.schema import meals
from nutrition_bot.domain.nutrient_gaps import FoodValue
from nutrition_bot.domain.supplement_reports import converted


async def food_values(
    connection: AsyncConnection, start: date, end: date, code: str, unit: str
) -> list[FoodValue]:
    rows = (
        (
            await connection.execute(
                sa.select(
                    revisions.c.local_date,
                    items.c.food_name,
                    items.c.quantity_method,
                    amounts.c.amount_scaled,
                    amounts.c.unit,
                    amounts.c.quality,
                )
                .select_from(
                    meals.join(revisions, meals.c.current_revision_id == revisions.c.id)
                    .join(items, items.c.revision_id == revisions.c.id)
                    .outerjoin(
                        amounts,
                        sa.and_(
                            amounts.c.revision_id == items.c.revision_id,
                            amounts.c.item_index == items.c.item_index,
                            amounts.c.nutrient_code == code,
                        ),
                    )
                )
                .where(
                    revisions.c.local_date.between(start, end),
                    revisions.c.sealed.is_(True),
                    revisions.c.deleted.is_(False),
                )
            )
        )
        .mappings()
        .all()
    )
    return [
        FoodValue(
            r["local_date"],
            r["food_name"],
            converted(r["amount_scaled"], r["unit"], unit),
            r["quality"] or "unknown",
            r["quantity_method"] == "approved_estimate",
        )
        for r in rows
    ]
