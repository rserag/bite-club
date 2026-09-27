"""Explicit batch-recipe commands; saving a definition never records consumption."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Literal

from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.recipes import (
    create_recipe,
    get_recipe,
    get_recipe_by_number,
    get_recipe_version,
    list_recipes,
    revise_recipe,
    validate_recipe_item,
)
from nutrition_bot.application.meal_conversation import MealReply, _short
from nutrition_bot.domain.drafts import DraftError, PlannedItem
from nutrition_bot.domain.food import MAX_INTEGER, milligrams_to_grams
from nutrition_bot.domain.meal_draft_text import ParsedDraftMeal, parse_draft_meal
from nutrition_bot.domain.meal_text import MealTextError, _parse_date
from nutrition_bot.domain.recipes import (
    RecipeDefinition,
    RecipeError,
    RecipeSnapshot,
    recipe_portions,
)
from nutrition_bot.domain.reuse import ReuseError

RecipeUnit = Literal["g", "serving"]
_REFERENCE = r"R([1-9][0-9]{0,18})(?:v([1-9][0-9]{0,18}))?"
_PINNED_REFERENCE = r"R[1-9][0-9]{0,18}v[1-9][0-9]{0,18}"
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
_PORTION = re.compile(
    rf"(?P<approx>(?:about|around|approximately|approx|roughly|estimated)\s+|~\s*)?"
    rf"(?P<amount>{_NUMBER})\s*(?P<unit>kg|mg|g|servings?)",
    re.IGNORECASE,
)
_INGREDIENT_ENVELOPE = re.compile(
    r"^(?:/|I\s+ate(?:\s|$)|today(?:\s|$)|yesterday(?:\s|$)|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s|$)|(?:breakfast|lunch|dinner|snack)\s*:)",
    re.IGNORECASE,
)
_PORTION_HELP = (
    "Enter one positive portion in g, kg, mg or servings, for example 300g or 1.5 servings. "
    "Use an explicit prefix such as 'about 300g' for an estimate."
)
_DEFINITION_HELP = (
    "Use explicit ingredient amounts followed by one batch definition: "
    "500g #1; about 20g #2 | yield 1200g, or 500g #1 | servings 4. "
    "Dates, meal labels and nested recipes do not belong in ingredient lists."
)


@dataclass(frozen=True, slots=True)
class _DefinitionText:
    ingredients: ParsedDraftMeal
    unit: RecipeUnit
    total_units: int
    estimate_basis: str | None


def parse_portion(text: str) -> tuple[RecipeUnit, int, str | None]:
    """Return integer milligrams or thousandths of a serving, without rounding."""
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 160
        or any(unicodedata.category(character).startswith("C") for character in text)
    ):
        raise RecipeError(_PORTION_HELP)
    matched = _PORTION.fullmatch(text.strip())
    if matched is None:
        raise RecipeError(_PORTION_HELP)
    source_unit = matched["unit"].casefold()
    unit: RecipeUnit = "serving" if source_unit.startswith("serving") else "g"
    factors = {"mg": 1, "g": 1000, "kg": 1_000_000, "serving": 1000, "servings": 1000}
    with localcontext() as context:
        context.prec = 200
        units = Decimal(matched["amount"]) * factors[source_unit]
    maximum = 50_000_000 if unit == "g" else 1_000_000
    if units != units.to_integral_value() or not 1 <= units <= maximum:
        raise RecipeError(
            "Use an exact milligram or thousandth of a serving, up to 50 kg or 1000 servings."
        )
    basis = "User-described approximate amount" if matched["approx"] else None
    return unit, int(units), basis


def _parse_definition(text: str, today: date) -> _DefinitionText:
    """Split explicit definition syntax, then consume the whole ingredient grammar."""
    if not isinstance(text, str) or not 1 <= len(text) <= 2000:
        raise RecipeError(_DEFINITION_HELP)
    parts = text.split("|")
    if len(parts) != 2:
        raise RecipeError(_DEFINITION_HELP)
    ingredient_text, denominator = (part.strip() for part in parts)
    if _INGREDIENT_ENVELOPE.match(ingredient_text):
        raise RecipeError(_DEFINITION_HELP)
    denominator_match = re.fullmatch(r"(yield|servings)\s+(.+)", denominator, re.IGNORECASE)
    if denominator_match is None:
        raise RecipeError(_DEFINITION_HELP)
    kind = denominator_match[1].casefold()
    amount = denominator_match[2] + (" servings" if kind == "servings" else "")
    unit, total_units, basis = parse_portion(amount)
    if (kind == "yield" and unit != "g") or (kind == "servings" and unit != "serving"):
        raise RecipeError(_DEFINITION_HELP)
    try:
        ingredients = parse_draft_meal(ingredient_text, today)
    except MealTextError:
        raise RecipeError(_DEFINITION_HELP) from None
    if (
        ingredients.label != "Meal"
        or ingredients.local_date != today
        or any(
            item.grams is None or re.fullmatch(_REFERENCE, item.query, re.IGNORECASE)
            for item in ingredients.items
        )
    ):
        raise RecipeError(_DEFINITION_HELP)
    return _DefinitionText(ingredients, unit, total_units, basis)


async def _definition(
    connection: AsyncConnection, text: str, reference: datetime
) -> RecipeDefinition:
    from nutrition_bot.application.draft_conversation import _planned

    parsed = _parse_definition(text, reference.date())
    # Every amount was explicit, so the shared resolver cannot infer history or portions.
    items = await _planned(connection, parsed.ingredients)
    return RecipeDefinition(
        items=items,
        unit=parsed.unit,
        total_units=parsed.total_units,
        estimate_basis=parsed.estimate_basis,
    )


def _reference_parts(text: str, *, require_version: bool = False) -> tuple[int, int | None]:
    matched = re.fullmatch(_REFERENCE, text, re.IGNORECASE)
    if matched is None:
        raise RecipeError("Choose a recipe reference, for example R1v1.")
    recipe_id = int(matched[1])
    version = int(matched[2]) if matched[2] is not None else None
    if recipe_id > MAX_INTEGER or (version is not None and version > MAX_INTEGER):
        raise RecipeError("Choose a valid recipe reference from /recipes.")
    if require_version and version is None:
        raise RecipeError("Choose the exact recipe version, for example R1v1.")
    return recipe_id, version


async def _target(
    connection: AsyncConnection,
    text: str,
    *,
    require_version: bool = False,
    current_only: bool = False,
    consuming: bool = False,
) -> RecipeSnapshot:
    recipe_id, number = _reference_parts(text, require_version=require_version)
    current = await get_recipe(connection, recipe_id)
    if current is None:
        raise RecipeError("That recipe does not exist. Use /recipes.")
    if current_only and number != current.version_number:
        raise RecipeError(f"That recipe changed. Open /recipe R{recipe_id} before updating it.")
    selected = (
        current if number is None else await get_recipe_by_number(connection, recipe_id, number)
    )
    if selected is None:
        raise RecipeError("That recipe version does not exist. Use /recipes.")
    if consuming and current.archived:
        raise RecipeError(f"Restore the recipe first: open /recipe R{recipe_id}.")
    return selected


async def recipe_view(
    connection: AsyncConnection, recipe: RecipeSnapshot, lead: str = "Recipe"
) -> MealReply:
    current = await get_recipe(connection, recipe.id)
    if current is None:
        raise RecipeError("This recipe is unavailable. Open /recipes again.")
    ref = f"R{recipe.id}v{recipe.version_number}"
    historical = recipe.version_id != current.version_id
    definition = recipe.definition
    total = format(milligrams_to_grams(definition.total_units), "f")
    lines = [f"{lead} {ref} · {_short(recipe.name, 60)}"]
    if historical:
        lines.append(
            f"Pinned historical batch; current recipe is R{current.id}v{current.version_number}."
        )
    if current.archived:
        lines.append("Recipe is archived; restore its current version before logging a portion.")
    lines.append(
        f"Whole batch: {total} "
        + ("g cooked yield" if definition.unit == "g" else "equal servings")
        + (" · estimated denominator" if definition.estimate_basis else "")
    )
    if definition.estimate_basis:
        lines.append("Basis: " + _short(" ".join(definition.estimate_basis.split()), 60))
    lines.append("Whole-batch ingredients:")
    uncertain = bool(definition.estimate_basis)
    for index, item in enumerate(definition.items, 1):
        food = await get_food_version(connection, item.food_version_id)
        assert item.edible_milligrams is not None
        lines.append(
            f"{index}. {milligrams_to_grams(item.edible_milligrams):f} g "
            f"{_short(food.record.name, 45)} ({food.record.preparation}; #{item.food_version_id})"
            + (" · estimated ingredient" if item.estimate_basis else "")
        )
        if item.estimate_basis:
            uncertain = True
            lines.append("   Basis: " + _short(" ".join(item.estimate_basis.split()), 60))
    lines.append(
        "Nutrition is calculated from these ingredients; no cooking-loss factors are inferred. "
        "Ingredient weights are not the cooked portion's weight."
    )
    if uncertain:
        lines.append("Every use requires fresh approval of the displayed estimates.")
    example = "300g" if definition.unit == "g" else "1 serving"
    lines.append(f"Log a portion: /recipe log {ref} {example}. Nothing was logged by this view.")
    buttons: tuple[str, ...]
    if historical:
        buttons = () if current.archived else ("portion",)
    else:
        buttons = ("restore",) if current.archived else ("portion", "archive")
    return MealReply(
        "\n".join(lines),
        "recipe_view",
        buttons=buttons,
        recipe_id=recipe.id,
        recipe_version_id=recipe.version_id,
    )


def _help() -> MealReply:
    return MealReply(
        "Save a recipe without logging a meal:\n"
        "/recipe create chili = 500g #1; about 20g #2 | yield 1200g\n"
        "Or define equal portions: /recipe create breakfast = 400g #1 | servings 4\n"
        "Open /recipe R1; log /recipe log R1v1 300g or /recipe log R2v1 1.5 servings.\n"
        "Update: /recipe update R1v1 = 500g #1 | yield 1100g\n"
        "Archive/restore: /recipe archive R1v1; /recipe restore R1v2. "
        "Use /recipes or /recipes all. Nothing was logged.",
        "recipe_help",
    )


async def handle_recipe_message(
    connection: AsyncConnection,
    message: Message,
    text: str,
    *,
    action_key: str,
    reference: datetime,
) -> MealReply | None:
    from nutrition_bot.adapters.database.meals import MealError
    from nutrition_bot.application.reuse_conversation import _consume

    if not re.match(r"/recipes?(?:\s|$)", text, re.IGNORECASE):
        return None
    try:
        if len(text) > 2000:
            raise RecipeError("Keep recipe commands below 2000 characters.")
        listing = re.fullmatch(r"/recipes(?:\s+(all))?", text, re.IGNORECASE)
        if listing:
            values = await list_recipes(connection, limit=11, include_archived=bool(listing[1]))
            lines = ["Recipes (up to 10 shown):"]
            lines.extend(
                f"R{item.id}v{item.version_number} · {item.name}"
                + (" · archived" if item.archived else "")
                for item in values[:10]
            )
            if not values:
                lines.append("No saved recipes.")
            if len(values) > 10:
                lines.append("More recipes exist; open a known R number directly.")
            lines.append("Open /recipe R1. Viewing a recipe does not log consumption.")
            return MealReply("\n".join(lines), "recipe_list")
        if text.casefold() == "/recipe":
            return _help()
        command = re.fullmatch(r"/recipe\s+(.+)", text, re.IGNORECASE | re.DOTALL)
        if command is None:
            raise RecipeError("Use /recipes or /recipes all to view your recipe list.")
        body = command[1]
        creating = re.fullmatch(r"create\s+(.+?)\s*=\s*(.+)", body, re.IGNORECASE | re.DOTALL)
        updating = re.fullmatch(
            rf"update\s+({_PINNED_REFERENCE})\s*=\s*(.+)", body, re.IGNORECASE | re.DOTALL
        )
        if creating:
            definition = await _definition(connection, creating[2], reference)
            saved = await create_recipe(connection, creating[1], definition, action_key=action_key)
            return await recipe_view(connection, saved, "Saved recipe; no meal logged")
        if updating:
            current = await _target(
                connection, updating[1], require_version=True, current_only=True
            )
            definition = await _definition(connection, updating[2], reference)
            saved = await revise_recipe(
                connection,
                current.id,
                current.version_id,
                action_key=action_key,
                definition=definition,
            )
            return await recipe_view(connection, saved, "Updated recipe; past meals unchanged")
        toggle = re.fullmatch(rf"(archive|restore)\s+({_PINNED_REFERENCE})", body, re.IGNORECASE)
        if toggle:
            current = await _target(connection, toggle[2], require_version=True, current_only=True)
            saved = await revise_recipe(
                connection,
                current.id,
                current.version_id,
                action_key=action_key,
                archived=toggle[1].casefold() == "archive",
            )
            return await recipe_view(connection, saved, "Updated recipe")
        logging = re.fullmatch(r"log\s+(.+)", body, re.IGNORECASE | re.DOTALL)
        if logging:
            day, request = _parse_date(logging[1], reference.date())
            selected = re.fullmatch(rf"({_PINNED_REFERENCE})\s+(.+)", request, re.IGNORECASE)
            if selected is None:
                raise RecipeError(
                    "Use /recipe log R1v1 300g or /recipe log yesterday R1v1 1 serving."
                )
            recipe = await _target(connection, selected[1], require_version=True, consuming=True)
            unit, units, basis = parse_portion(selected[2])
            if unit != recipe.definition.unit:
                raise RecipeError(
                    "Use the recipe's defined unit; grams and servings cannot be inferred."
                )
            return await _consume(
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
        if re.fullmatch(_REFERENCE, body, re.IGNORECASE):
            return await recipe_view(connection, await _target(connection, body))
        raise RecipeError(
            "Use /recipe for creation, logging and update examples. Nothing was logged."
        )
    except (RecipeError, MealError, DraftError, MealTextError, ReuseError) as error:
        return MealReply(_short(str(error), 1800) + "\nNothing was changed.", "meal_rejected")
    except ValidationError:
        return MealReply(
            "This recipe definition or portion is invalid. Nothing was changed.", "meal_rejected"
        )


async def handle_recipe_callback(
    connection: AsyncConnection,
    action: str,
    recipe_id: int,
    version_id: int,
    *,
    action_key: str,
) -> MealReply:
    from nutrition_bot.adapters.database.meals import MealError

    try:
        current = await get_recipe(connection, recipe_id)
        selected = await get_recipe_version(connection, version_id)
        if current is None or selected is None or selected.id != recipe_id:
            raise RecipeError("This recipe is unavailable. Open /recipes again.")
        if action == "portion":
            return await recipe_view(connection, selected, "Choose an explicit portion of recipe")
        if action not in {"archive", "restore"}:
            raise RecipeError("Unknown recipe action.")
        if current.version_id != version_id:
            raise RecipeError(f"That recipe button is stale. Open /recipe R{recipe_id}.")
        updated = await revise_recipe(
            connection,
            recipe_id,
            version_id,
            action_key=action_key,
            archived=action == "archive",
        )
        return await recipe_view(connection, updated, "Updated recipe")
    except (RecipeError, MealError) as error:
        return MealReply(_short(str(error), 1800) + "\nNothing was changed.", "meal_rejected")
    except ValidationError:
        return MealReply("This recipe is invalid. Nothing was changed.", "meal_rejected")


async def revised_recipe_items(
    connection: AsyncConnection, existingitems: tuple[PlannedItem, ...], text: str
) -> tuple[PlannedItem, ...]:
    """Recalculate one whole recipe group from its immutable original definition."""
    correction = re.fullmatch(r"portion\s+(.+)", text, re.IGNORECASE)
    if correction is None:
        raise RecipeError(
            "Use 'portion 300g' or 'portion 1.5 servings' to change a recipe portion."
        )
    unit, units, basis = parse_portion(correction[1])
    error = "Use a portion correction only for one complete recipe portion."
    if not isinstance(existingitems, tuple) or not existingitems:
        raise RecipeError(error)
    shares = [item.recipe_share for item in existingitems]
    first = shares[0]
    if first is None or any(share is None for share in shares):
        raise RecipeError(error)
    expected = first.model_dump(exclude={"ingredient_index"})
    if any(
        share is None or share.model_dump(exclude={"ingredient_index"}) != expected
        for share in shares
    ):
        raise RecipeError(error)
    recipe = await get_recipe_version(connection, first.version_id)
    if recipe is None or len(existingitems) != len(recipe.definition.items):
        raise RecipeError(error)
    if [share.ingredient_index for share in shares if share is not None] != list(
        range(len(shares))
    ):
        raise RecipeError(error)
    for item in existingitems:
        await validate_recipe_item(connection, item)
    if unit != recipe.definition.unit:
        raise RecipeError(
            "Use the original recipe's portion unit; grams and servings cannot be inferred."
        )
    return recipe_portions(recipe, units, estimate_basis=basis)
