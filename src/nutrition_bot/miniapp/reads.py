"""Bounded, read-only views over existing immutable application snapshots."""

from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.checkins import food_day_status
from nutrition_bot.adapters.database.daily import daily_totals
from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.goals import current_plan
from nutrition_bot.adapters.database.meals import get_meal
from nutrition_bot.adapters.database.schema import food_versions, meal_revisions, meals
from nutrition_bot.adapters.database.schema_favorites import favorite_versions, favorites
from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights
from nutrition_bot.domain.food import milligrams_to_grams


def amount(scaled: int | None) -> str | None:
    if scaled is None:
        return None
    with localcontext() as context:
        context.prec = 80
        return format(Decimal(scaled) / 1_000_000, "f")


async def dashboard(connection: AsyncConnection, day: date, timezone: str) -> dict[str, Any]:
    totals = await daily_totals(connection, day)
    status = await food_day_status(connection, day)
    target = await current_plan(connection, on_date=day)
    series = []
    for offset in range(6, -1, -1):
        history_day = day - timedelta(days=offset)
        history = await daily_totals(connection, history_day)
        energy = next((item for item in history.nutrients if item.code == "energy"), None)
        completeness = await food_day_status(connection, history_day)
        series.append(
            {
                "date": history_day.isoformat(),
                "energy": "0"
                if not history.item_count and completeness.state == "complete"
                else amount(energy.known_amount_scaled)
                if energy
                else None,
                "partial": bool(energy and energy.missing_items),
                "status": completeness.state,
            }
        )
    weights = (
        (
            await connection.execute(
                sa.select(
                    body_weight_revisions.c.local_date,
                    body_weight_revisions.c.weight_grams,
                )
                .select_from(
                    body_weights.join(
                        body_weight_revisions,
                        body_weights.c.current_revision_id == body_weight_revisions.c.id,
                    )
                )
                .where(body_weight_revisions.c.deleted.is_(False))
                .order_by(body_weight_revisions.c.local_date.desc(), body_weights.c.id.desc())
                .limit(14)
            )
        )
        .mappings()
        .all()
    )
    favorite_rows = (
        (
            await connection.execute(
                sa.select(
                    favorites.c.id,
                    favorites.c.name,
                    favorite_versions.c.version_number,
                )
                .select_from(
                    favorites.join(
                        favorite_versions, favorites.c.current_version_id == favorite_versions.c.id
                    )
                )
                .where(
                    favorite_versions.c.archived.is_(False), favorite_versions.c.sealed.is_(True)
                )
                .order_by(favorites.c.name)
                .limit(12)
            )
        )
        .mappings()
        .all()
    )
    return {
        "date": day.isoformat(),
        "timezone": timezone,
        "meal_count": len(totals.meals),
        "item_count": totals.item_count,
        "status": status.state,
        "unresolved_drafts": status.unresolved_drafts,
        "nutrients": [
            {
                "code": item.code,
                "name": item.name,
                "unit": item.unit,
                "known": "0"
                if not totals.item_count and status.state == "complete"
                else amount(item.known_amount_scaled),
                "known_items": item.known_items,
                "total_items": item.total_items,
                "missing_items": item.missing_items,
            }
            for item in totals.nutrients
        ],
        "target": asdict(target) | {"effective_from": target.effective_from.isoformat()}
        if target
        else None,
        "energy_history": series,
        "weight_history": [
            {
                "date": item["local_date"].isoformat(),
                "kg": str(Decimal(item["weight_grams"]) / 1000),
            }
            for item in reversed(weights)
        ],
        "favorites": [
            {"name": item["name"], "reference": f"F{item['id']}v{item['version_number']}"}
            for item in favorite_rows
        ],
    }


async def food_choices(connection: AsyncConnection, query: str) -> list[dict[str, Any]]:
    latest = (
        sa.select(food_versions.c.food_id, sa.func.max(food_versions.c.id).label("version_id"))
        .where(food_versions.c.sealed.is_(True))
        .group_by(food_versions.c.food_id)
        .subquery()
    )
    statement = (
        sa.select(food_versions.c.id)
        .join(latest, latest.c.version_id == food_versions.c.id)
        .order_by(food_versions.c.name, food_versions.c.id)
        .limit(30)
    )
    if query:
        # contains(autoescape=True) treats user's % and _ as literal search text.
        statement = statement.where(
            sa.func.unicode_casefold(food_versions.c.name).contains(
                query.casefold(), autoescape=True
            )
        )
    ids = (await connection.execute(statement)).scalars().all()
    result = []
    for version_id in ids:
        food = await get_food_version(connection, version_id)
        result.append(
            {
                "version_id": food.version_id,
                "name": food.record.name,
                "brand": food.record.brand,
                "preparation": food.record.preparation,
                "source": food.record.source_reference,
                "nutrients": [
                    {"code": item.code, "unit": item.unit, "per_100g": amount(item.amount_scaled)}
                    for item in food.nutrients
                ],
            }
        )
    return result


async def meal_history(connection: AsyncConnection, before: int | None) -> list[dict[str, Any]]:
    statement = (
        sa.select(meals.c.id)
        .join(meal_revisions, meals.c.current_revision_id == meal_revisions.c.id)
        .order_by(meals.c.id.desc())
        .limit(20)
    )
    if before is not None:
        statement = statement.where(meals.c.id < before)
    ids = (await connection.execute(statement)).scalars().all()
    result = []
    for meal_id in ids:
        meal = await get_meal(connection, meal_id)
        result.append(
            {
                "id": meal.id,
                "reference": f"M{meal.id}r{meal.revision_number}",
                "label": meal.label,
                "date": meal.local_date.isoformat(),
                "deleted": meal.deleted,
                "items": [
                    {
                        "name": item.food_name,
                        "version_id": item.food_version_id,
                        "grams": str(milligrams_to_grams(item.edible_milligrams)),
                        "estimated": item.quantity_method == "approved_estimate",
                    }
                    for item in meal.items
                ],
            }
        )
    return result
