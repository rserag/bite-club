"""Aggregate immutable consumed amounts from current, active meal revisions."""

from collections import Counter, defaultdict
from datetime import date
from typing import cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import (
    meal_item_nutrients,
    meal_items,
    meal_revisions,
    meals,
    nutrients,
)
from nutrition_bot.domain.daily import (
    CORE_NUTRIENT_ORDER,
    DailyMeal,
    DailyNutrient,
    DailyTotals,
)
from nutrition_bot.domain.food import Unit


async def daily_totals(connection: AsyncConnection, local_date: date) -> DailyTotals:
    """Use each meal's stored local date, without re-bucketing historical meals.

    The caller owns the transaction. One joined read captures all meal snapshots;
    the nutrient registry supplies definitions, never food nutritional values.
    Python integers keep totals exact even when their sum exceeds SQLite int64.
    """
    definitions = (await connection.execute(sa.select(nutrients))).mappings().all()
    rows = (
        (
            await connection.execute(
                sa.select(
                    meals.c.id.label("meal_id"),
                    meal_revisions.c.revision_number,
                    meal_revisions.c.label,
                    meal_revisions.c.timezone,
                    meal_items.c.revision_id,
                    meal_items.c.item_index,
                    meal_items.c.source_kind,
                    meal_items.c.quantity_method,
                    meal_items.c.recipe_version_id,
                    meal_item_nutrients.c.nutrient_code,
                    meal_item_nutrients.c.amount_scaled,
                    meal_item_nutrients.c.quality,
                )
                .select_from(
                    meals.join(meal_revisions, meals.c.current_revision_id == meal_revisions.c.id)
                    .join(meal_items, meal_items.c.revision_id == meal_revisions.c.id)
                    .outerjoin(
                        meal_item_nutrients,
                        sa.and_(
                            meal_item_nutrients.c.revision_id == meal_items.c.revision_id,
                            meal_item_nutrients.c.item_index == meal_items.c.item_index,
                        ),
                    )
                )
                .where(
                    meal_revisions.c.local_date == local_date,
                    meal_revisions.c.sealed.is_(True),
                    meal_revisions.c.deleted.is_(False),
                )
                .order_by(
                    meal_revisions.c.consumed_at,
                    meals.c.id,
                    meal_items.c.item_index,
                    meal_item_nutrients.c.nutrient_code,
                )
            )
        )
        .mappings()
        .all()
    )
    daily_meals: dict[int, DailyMeal] = {}
    item_keys: set[tuple[int, int]] = set()
    sources: Counter[str] = Counter()
    quantities: Counter[str] = Counter()
    timezones: set[str] = set()
    amounts: dict[str, int] = defaultdict(int)
    known: Counter[str] = Counter()
    qualities: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        meal_id = row["meal_id"]
        if meal_id not in daily_meals:
            daily_meals[meal_id] = DailyMeal(meal_id, row["revision_number"], row["label"])
            timezones.add(row["timezone"])
        item_key = (row["revision_id"], row["item_index"])
        if item_key not in item_keys:
            item_keys.add(item_key)
            sources[row["source_kind"]] += 1
            method = row["quantity_method"]
            if method == "measured" and row["recipe_version_id"] is not None:
                method = "calculated_recipe"
            quantities[method] += 1
        amount = row["amount_scaled"]
        if amount is not None:
            code = row["nutrient_code"]
            amounts[code] += amount
            known[code] += 1
            qualities[code][row["quality"]] += 1

    order = {code: index for index, code in enumerate(CORE_NUTRIENT_ORDER)}
    definitions = sorted(
        definitions, key=lambda value: (order.get(value["code"], len(order)), value["code"])
    )
    return DailyTotals(
        local_date=local_date,
        meals=tuple(daily_meals.values()),
        item_count=len(item_keys),
        nutrients=tuple(
            DailyNutrient(
                code=value["code"],
                name=value["name"],
                unit=cast(Unit, value["unit"]),
                known_amount_scaled=amounts[value["code"]] if known[value["code"]] else None,
                known_items=known[value["code"]],
                total_items=len(item_keys),
                quality_counts=tuple(sorted(qualities[value["code"]].items())),
            )
            for value in definitions
        ),
        source_counts=tuple(sorted(sources.items())),
        quantity_counts=tuple(sorted(quantities.items())),
        recorded_timezones=tuple(sorted(timezones)),
    )
