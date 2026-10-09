"""Guided batch definitions and portions, using the existing immutable recipe ledger."""

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.meals import MealError
from nutrition_bot.adapters.database.recipes import (
    create_recipe,
    get_recipe,
    get_recipe_version,
    revise_recipe,
)
from nutrition_bot.adapters.database.schema_recipes import recipe_versions, recipes
from nutrition_bot.application.meal_conversation import MealReply, _short
from nutrition_bot.application.recipe_conversation import parse_portion
from nutrition_bot.application.reuse_conversation import _consume
from nutrition_bot.domain.drafts import DraftError, PlannedItem
from nutrition_bot.domain.food import milligrams_to_grams
from nutrition_bot.domain.meal_text import MealTextError, _parse_date
from nutrition_bot.domain.recipes import (
    RecipeDefinition,
    RecipeError,
    RecipeSnapshot,
    normalize_recipe_name,
    recipe_display_name,
    recipe_portions,
)
from nutrition_bot.domain.reuse import ReuseError

Flow = Mapping[str, Any]
Payload = dict[str, Any]
_ERRORS = (RecipeError, MealError, MealTextError, DraftError, ReuseError, ValidationError)


def _button(revision: int, label: str, action: str) -> tuple[str, str]:
    return label, f"flow:{revision}:recipe:{action}"


async def _advance(
    connection: AsyncConnection,
    owner_id: int,
    stage: str,
    payload: Payload,
    text: str,
    actions: tuple[tuple[str, str], ...] = (),
) -> MealReply:
    from nutrition_bot.application.navigation import begin, menu

    revision = await begin(connection, owner_id, "recipe_" + stage, payload)
    return menu(
        text,
        tuple(_button(revision, label, action) for label, action in actions)
        + (("Cancel", "cancel"),),
    )


async def start(connection: AsyncConnection, owner_id: int, page: int = 0) -> MealReply:
    """Show active saved batches, with pinned choices and bounded pagination."""
    rows = (
        await connection.execute(
            sa.select(recipe_versions.c.id, recipes.c.name)
            .join(recipes, recipes.c.current_version_id == recipe_versions.c.id)
            .where(recipe_versions.c.sealed.is_(True), recipe_versions.c.archived.is_(False))
            .order_by(recipes.c.normalized_name, recipes.c.id)
            .offset(page * 8)
            .limit(9)
        )
    ).all()
    actions = tuple((_short(row.name, 40), f"open={row.id}") for row in rows[:8])
    if page:
        actions += (("Previous recipes", f"page={page - 1}"),)
    if len(rows) > 8:
        actions += (("More recipes", f"page={page + 1}"),)
    actions += (("Create recipe", "new"),)
    return await _advance(
        connection,
        owner_id,
        "list",
        {"choices": [row.id for row in rows[:8]], "page": page, "has_more": len(rows) > 8},
        "Recipes\nChoose a saved batch to log a portion or create a recipe."
        if rows
        else "Recipes\nNo saved recipes yet. Create a batch, then log its portions.",
        actions,
    )


async def _new(connection: AsyncConnection, owner_id: int) -> MealReply:
    return await _advance(
        connection,
        owner_id,
        "name",
        {"ingredients": []},
        "What would you like to call this recipe? E.g. bean stew. Saving a recipe records "
        "its ingredients and batch size; it does not log a meal.",
    )


async def _search(connection: AsyncConnection, owner_id: int, payload: Payload) -> MealReply:
    return await _advance(
        connection,
        owner_id,
        "search",
        payload,
        "Which ingredient would you like to add? Type its food name and preparation, "
        "e.g. raw carrot. Use the preparation that matches its ingredient weight.",
    )


async def selected_food(
    connection: AsyncConnection,
    payload: Payload,
    version_id: int,
    name: str,
    *,
    owner_id: int,
    **context: Any,
) -> MealReply:
    """Resume recipe entry after an exact reviewed food selection."""
    food = await get_food_version(connection, version_id)
    if food.record.preparation == "unspecified":
        raise RecipeError("Choose an ingredient with a clear raw, cooked or packaged preparation.")
    state = dict(payload)
    state["selected_food_id"] = version_id
    return await _advance(
        connection,
        owner_id,
        "amount",
        state,
        f"How much {_short(food.record.name, 80)} ({food.record.preparation}) went into "
        "the whole batch? Send edible grams, e.g. 500 g. Use about 500 g for an estimate.",
    )


async def _ingredient_lines(connection: AsyncConnection, payload: Payload) -> list[str]:
    lines = []
    for index, value in enumerate(payload["ingredients"], 1):
        item = PlannedItem.model_validate(value)
        food = await get_food_version(connection, item.food_version_id)
        assert item.edible_milligrams is not None
        lines.append(
            f"{index}. {milligrams_to_grams(item.edible_milligrams):f} g "
            f"{_short(food.record.name, 60)} · {food.record.preparation}"
            + (" · estimated" if item.estimate_basis else "")
        )
    return lines


async def _ingredients(connection: AsyncConnection, owner_id: int, payload: Payload) -> MealReply:
    actions: tuple[tuple[str, str], ...] = (
        () if len(payload["ingredients"]) >= 10 else (("Add ingredient", "more"),)
    )
    if payload["ingredients"]:
        actions += (("Set batch size", "batch"), ("Remove last ingredient", "remove"))
    return await _advance(
        connection,
        owner_id,
        "ingredients",
        payload,
        f"{payload['recipe_name']} — whole-batch ingredients\n"
        + "\n".join(await _ingredient_lines(connection, payload))
        + "\nNothing has been logged. Include oil or sauce you want recorded.",
        actions,
    )


async def _batch(connection: AsyncConnection, owner_id: int, payload: Payload) -> MealReply:
    return await _advance(
        connection,
        owner_id,
        "batch",
        payload,
        "How will you measure portions of this batch? Choose its actual edible cooked "
        "weight or a declared number of equal servings. Ingredient weights do not "
        "establish cooked yield.",
        (("Cooked batch weight", "unit=g"), ("Equal servings", "unit=serving")),
    )


async def _review(connection: AsyncConnection, owner_id: int, payload: Payload) -> MealReply:
    definition = _definition(payload)
    size = f"{milligrams_to_grams(definition.total_units):f} " + (
        "g edible cooked yield" if definition.unit == "g" else "equal servings"
    )
    uncertain = bool(definition.estimate_basis or any(i.estimate_basis for i in definition.items))
    return await _advance(
        connection,
        owner_id,
        "review",
        payload,
        f"Review recipe: {payload['recipe_name']}\n"
        + "\n".join(await _ingredient_lines(connection, payload))
        + f"\nWhole batch: {size}"
        + (" · estimated batch size" if definition.estimate_basis else "")
        + "\nNutrition comes from these ingredients; cooking losses are not inferred. "
        "Save the batch, then enter the portion you actually ate."
        + (" Every use will require fresh estimate approval." if uncertain else ""),
        (
            ("Save recipe", "save"),
            ("Change ingredients", "ingredients"),
            ("Change batch size", "batch"),
        ),
    )


def _definition(payload: Payload) -> RecipeDefinition:
    return RecipeDefinition(
        items=tuple(PlannedItem.model_validate(item) for item in payload["ingredients"]),
        unit=payload["unit"],
        total_units=payload["total_units"],
        estimate_basis=payload.get("estimate_basis"),
    )


async def _view(
    connection: AsyncConnection, owner_id: int, recipe: RecipeSnapshot, lead: str = "Recipe"
) -> MealReply:
    current = await get_recipe(connection, recipe.id)
    if current is None or current.archived:
        raise RecipeError("This recipe is unavailable or archived. Open Recipes again.")
    batch = f"{milligrams_to_grams(recipe.definition.total_units):f} " + (
        "g cooked yield" if recipe.definition.unit == "g" else "equal servings"
    )
    payload = {
        "recipe_id": recipe.id,
        "recipe_version_id": recipe.version_id,
        "recipe_name": recipe.name,
        "ingredients": [item.model_dump(mode="json") for item in recipe.definition.items],
    }
    text = f"{lead}: {recipe.name}\nWhole batch: {batch}\n"
    text += "\n".join(await _ingredient_lines(connection, payload))
    if recipe.definition.estimate_basis or any(i.estimate_basis for i in recipe.definition.items):
        text += "\nContains estimates; each portion needs fresh approval."
    if current.version_id != recipe.version_id:
        text += "\nThis is the selected earlier batch. New batch changes use the current recipe."
    text += "\nNo meal has been logged by saving or viewing this recipe."
    actions: tuple[tuple[str, str], ...] = (("Log portion", "portion"),)
    if current.version_id == recipe.version_id:
        actions += (("New batch", "edit"),)
    actions += (("Recipes", "list"),)
    result = await _advance(connection, owner_id, "view", payload, text, actions)
    from dataclasses import replace

    return replace(
        result,
        ui_buttons=result.ui_buttons[:-1]
        + (("Details", f"command:/recipe R{recipe.id}v{recipe.version_number}"),)
        + result.ui_buttons[-1:],
    )


async def start_portion(
    connection: AsyncConnection, owner_id: int, recipe_id: int, version_id: int
) -> MealReply:
    recipe = await get_recipe_version(connection, version_id)
    current = await get_recipe(connection, recipe_id)
    if recipe.id != recipe_id or current is None or current.archived:
        raise RecipeError("This recipe is unavailable or archived. Open Recipes again.")
    size = f"{milligrams_to_grams(recipe.definition.total_units):f} " + (
        "g" if recipe.definition.unit == "g" else "equal servings"
    )
    example = "250 g" if recipe.definition.unit == "g" else "1.5 servings"
    return await _advance(
        connection,
        owner_id,
        "portion",
        {"recipe_id": recipe_id, "recipe_version_id": version_id},
        f"How much {recipe.name} did you eat?\nSelected batch: {size}. Send {example}, "
        f"or about {example} for an estimate. You can include yesterday or a past date. "
        "Grams and servings cannot be converted without the batch definition.",
    )


def _amount(text: str, unit: str) -> tuple[str, int, str | None]:
    if re.fullmatch(
        r"(?:(?:about|around|roughly|approximately|approx|estimated)\s+|~\s*)?"
        r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)",
        text,
        re.IGNORECASE,
    ):
        text += " g" if unit == "g" else " servings"
    return parse_portion(text)


async def guide_message(
    connection: AsyncConnection,
    row: Flow,
    text: str,
    message: Message,
    *,
    action_key: str,
    reference: datetime,
    owner_id: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import cancel, menu

    payload = dict(row["payload"])
    stage = row["stage"]
    try:
        if stage == "recipe_name":
            name = recipe_display_name(text)
            existing = await connection.scalar(
                sa.select(recipes.c.id).where(
                    recipes.c.normalized_name == normalize_recipe_name(name)
                )
            )
            if existing is not None:
                raise RecipeError(
                    "That recipe name already exists. Open it in Recipes for a new batch, "
                    "or choose a different name."
                )
            payload["recipe_name"] = name
            return await _search(connection, owner_id, payload)
        if stage == "recipe_search":
            from nutrition_bot.application.food_discovery import start_search

            return await start_search(
                connection,
                text,
                payload | {"continuation": "recipe"},
                action_key=action_key,
                owner_id=owner_id,
                chat_id=message.chat.id,
                remote_enabled=bool(context.get("food_discovery_remote_enabled", True)),
            )
        if stage == "recipe_amount":
            unit, units, basis = _amount(text, "g")
            if unit != "g":
                raise RecipeError("Enter the whole-batch ingredient's edible weight in grams.")
            item = PlannedItem(
                food_version_id=payload["selected_food_id"],
                edible_milligrams=units,
                original_quantity=format(milligrams_to_grams(units), "f"),
                original_unit="g",
                estimate_basis=basis,
            )
            payload["ingredients"] = payload["ingredients"] + [item.model_dump(mode="json")]
            payload.pop("selected_food_id", None)
            return await _ingredients(connection, owner_id, payload)
        if stage == "recipe_denominator":
            unit, units, basis = _amount(text, payload["unit"])
            if unit != payload["unit"]:
                raise RecipeError(
                    "Use the batch unit you selected, or choose the other batch type."
                )
            payload.update(total_units=units, estimate_basis=basis)
            return await _review(connection, owner_id, payload)
        if stage == "recipe_portion":
            recipe = await get_recipe_version(connection, payload["recipe_version_id"])
            current = await get_recipe(connection, payload["recipe_id"])
            if recipe.id != payload["recipe_id"] or current is None or current.archived:
                raise RecipeError("This recipe is unavailable or archived. Open Recipes again.")
            day, amount = _parse_date(text, reference.date())
            unit, units, basis = _amount(amount, recipe.definition.unit)
            if unit != recipe.definition.unit:
                raise RecipeError(
                    "Use the selected batch's portion unit; grams and servings cannot be inferred."
                )
            result = await _consume(
                connection,
                recipe_portions(recipe, units, estimate_basis=basis),
                label=recipe.name,
                factor=Decimal(1),
                day=day,
                reference=reference,
                message=message,
                action_key=action_key,
                lead=f"recipe R{recipe.id}v{recipe.version_number}",
            )
            await cancel(connection, owner_id)
            return result
        return menu("Choose a recipe action from the latest buttons.", (("Cancel", "cancel"),))
    except _ERRORS as error:
        return menu(
            _short(str(error), 1000) + "\nThis step is still open; try again or cancel.",
            (("Cancel", "cancel"),),
        )


async def guide_action(
    connection: AsyncConnection,
    row: Flow,
    operation: str,
    message: Message,
    *,
    action_key: str,
    owner_id: int,
    **context: Any,
) -> MealReply:
    from nutrition_bot.application.navigation import menu

    payload = dict(row["payload"])
    stage = row["stage"]
    operation = operation.removeprefix("recipe:")
    try:
        if operation == "new" and stage == "recipe_list":
            return await _new(connection, owner_id)
        if operation == "list" and stage == "recipe_view":
            return await start(connection, owner_id)
        if operation.startswith("page=") and stage == "recipe_list":
            page = int(operation[5:])
            if (
                page == payload["page"] - 1
                and page >= 0
                or (page == payload["page"] + 1 and payload["has_more"])
            ):
                return await start(connection, owner_id, page)
        if operation.startswith("open=") and stage == "recipe_list":
            version_id = int(operation[5:])
            if version_id in payload["choices"]:
                return await _view(
                    connection, owner_id, await get_recipe_version(connection, version_id)
                )
        if operation in {"portion", "edit"} and stage == "recipe_view":
            if operation == "portion":
                return await start_portion(
                    connection, owner_id, payload["recipe_id"], payload["recipe_version_id"]
                )
            current = await get_recipe(connection, payload["recipe_id"])
            if (
                current is None
                or current.archived
                or current.version_id != payload["recipe_version_id"]
            ):
                raise RecipeError("This recipe changed. Open the current recipe for a new batch.")
            return await _ingredients(connection, owner_id, payload)
        if (
            operation == "more"
            and stage == "recipe_ingredients"
            and len(payload["ingredients"]) < 10
        ):
            return await _search(connection, owner_id, payload)
        if operation == "remove" and stage == "recipe_ingredients" and payload["ingredients"]:
            payload["ingredients"] = payload["ingredients"][:-1]
            return await _ingredients(connection, owner_id, payload)
        if operation == "ingredients" and stage == "recipe_review":
            return await _ingredients(connection, owner_id, payload)
        if (
            operation == "batch"
            and stage in {"recipe_ingredients", "recipe_review"}
            and payload["ingredients"]
        ):
            return await _batch(connection, owner_id, payload)
        if operation in {"unit=g", "unit=serving"} and stage == "recipe_batch":
            payload["unit"] = operation[5:]
            return await _advance(
                connection,
                owner_id,
                "denominator",
                payload,
                "What is the actual edible cooked batch weight? E.g. 1200 g. "
                "If estimated, say about 1200 g."
                if payload["unit"] == "g"
                else "How many equal servings does the whole batch contain? E.g. 4 servings. "
                "If estimated, say about 4 servings.",
            )
        if operation == "save" and stage == "recipe_review":
            definition = _definition(payload)
            saved = (
                await revise_recipe(
                    connection,
                    payload["recipe_id"],
                    payload["recipe_version_id"],
                    action_key=action_key,
                    definition=definition,
                )
                if "recipe_id" in payload
                else await create_recipe(
                    connection, payload["recipe_name"], definition, action_key=action_key
                )
            )
            return await _view(connection, owner_id, saved, "Saved recipe; no meal logged")
        return menu(
            "That recipe step changed. Use the latest recipe buttons.", (("Cancel", "cancel"),)
        )
    except _ERRORS as error:
        return menu(
            _short(str(error), 1000) + "\nNothing was changed.",
            (("Recipes", "recipes"), ("Cancel", "cancel")),
        )
