"""Bounded, read-only views over existing immutable application snapshots."""

from collections.abc import Sequence
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
from nutrition_bot.adapters.database.meals import MealItemSnapshot, MealSnapshot, get_meal
from nutrition_bot.adapters.database.recipes import get_recipe_version
from nutrition_bot.adapters.database.schema import food_versions, foods, meal_revisions, meals
from nutrition_bot.adapters.database.schema_favorites import favorite_versions, favorites
from nutrition_bot.adapters.database.schema_weights import body_weight_revisions, body_weights
from nutrition_bot.application.food_catalog import local_food_search
from nutrition_bot.application.recipe_display import portion_mass
from nutrition_bot.domain.food import Preparation, milligrams_to_grams
from nutrition_bot.domain.recipe_portions import ingredient_grams


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


async def food_choices(
    connection: AsyncConnection, query: str, *, preparation: Preparation | None = None
) -> list[dict[str, Any]]:
    latest = (
        sa.select(food_versions.c.food_id, sa.func.max(food_versions.c.id).label("version_id"))
        .where(food_versions.c.sealed.is_(True))
        .group_by(food_versions.c.food_id)
        .subquery()
    )
    statement = (
        sa.select(food_versions.c.id)
        .join(latest, latest.c.version_id == food_versions.c.id)
        .join(foods, foods.c.id == food_versions.c.food_id)
        .order_by(food_versions.c.name, food_versions.c.id)
        .limit(30)
    )
    if preparation:
        statement = statement.where(foods.c.preparation == preparation)
    ids: Sequence[int]
    if query:
        matches = await local_food_search(connection, query, preparation=preparation, limit=30)
        ids = [item.version_id for item in matches]
    else:
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
    ids: Sequence[int] = (await connection.execute(statement)).scalars().all()
    result = []
    for meal_id in ids:
        meal = await get_meal(connection, meal_id)
        recipes, recipe_editable = await recipe_choices(connection, meal)
        result.append(
            {
                "id": meal.id,
                "reference": f"M{meal.id}r{meal.revision_number}",
                "label": meal.label,
                "date": meal.local_date.isoformat(),
                "deleted": meal.deleted,
                "items": [meal_item(item) for item in meal.items],
                "recipes": recipes,
                "recipe_editable": recipe_editable,
            }
        )
    return result


def meal_item(item: MealItemSnapshot) -> dict[str, Any]:
    share = item.recipe_share
    return {
        "name": item.food_name,
        "version_id": item.food_version_id,
        "preparation": item.preparation,
        "grams": str(
            ingredient_grams(share, item.edible_milligrams)
            if share
            else milligrams_to_grams(item.edible_milligrams)
        ),
        "quantity_text": portion_mass(item.edible_milligrams, share),
        "recipe_ingredient": share is not None,
        "estimated": item.quantity_method == "approved_estimate",
    }


async def recipe_choices(
    connection: AsyncConnection, meal: MealSnapshot
) -> tuple[list[dict[str, Any]], bool]:
    groups: dict[str, dict[str, Any]] = {}
    shares = [item.recipe_share for item in meal.items]
    for share in shares:
        if share is None:
            continue
        key = share.model_dump_json(exclude={"ingredient_index"})
        if key in groups:
            continue
        recipe = await get_recipe_version(connection, share.version_id)
        groups[key] = {
            "reference": f"R{share.recipe_id}v{share.version_number}",
            "version_id": share.version_id,
            "name": share.name,
            "unit": share.unit,
            "amount": str(Decimal(share.portion_units) / 1000),
            "maximum": str(Decimal(share.total_units) / 1000),
            "fraction": f"{share.fraction.numerator}/{share.fraction.denominator}",
            "estimated": bool(share.portion_estimate_basis),
            "estimated_batch": bool(recipe.definition.estimate_basis)
            or any(item.estimate_basis for item in recipe.definition.items),
        }
    first = shares[0] if shares else None
    editable = False
    if first is not None and len(groups) == 1 and all(shares):
        recipe = await get_recipe_version(connection, first.version_id)
        editable = len(meal.items) == len(recipe.definition.items) and [
            share.ingredient_index for share in shares if share is not None
        ] == list(range(len(shares)))
    return list(groups.values()), editable
