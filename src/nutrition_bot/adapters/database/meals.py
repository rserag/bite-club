"""Immutable meal ledger. The caller owns one Store.write transaction and action row."""

import math
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import localcontext
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.foods import get_food_version
from nutrition_bot.adapters.database.schema import (
    actions,
    meal_item_nutrients,
    meal_items,
    meal_revisions,
    meals,
)
from nutrition_bot.domain.food import MAX_INTEGER, FrozenModel, Unit, exact_decimal
from nutrition_bot.domain.food_source import SourceProvenance
from nutrition_bot.domain.recipe_portions import RecipeError, RecipeShare

Operation = Literal["create", "edit", "delete", "undo"]


class MealError(ValueError):
    """A safe, actionable message that the Telegram interface can display."""


@dataclass(frozen=True, slots=True)
class MealItemInput:
    food_version_id: int
    edible_milligrams: int
    original_quantity: str
    original_unit: str
    quantity_method: Literal["measured", "approved_estimate"] = "measured"
    quantity_basis: str | None = None
    approval_action_key: str | None = None
    approved_at: float | None = None
    approval_draft_id: int | None = None
    approval_draft_revision: int | None = None
    recipe_share: RecipeShare | None = None


class MealNutrientSnapshot(FrozenModel):
    code: str
    unit: Unit
    amount_scaled: int | None
    quality: str


class MealItemSnapshot(FrozenModel):
    food_version_id: int
    food_name: str
    preparation: Literal["raw", "cooked", "as_sold", "as_prepared"]
    source_kind: str
    source_reference: str
    source_url: str | None
    source_license: str
    food_content_sha256: str
    calculation_version: str
    provenance: SourceProvenance | None
    edible_milligrams: int
    original_quantity: str
    original_unit: str
    quantity_method: Literal["measured", "approved_estimate"] = "measured"
    quantity_basis: str | None = None
    approval_action_key: str | None = None
    approved_at: float | None = None
    approval_draft_id: int | None = None
    approval_draft_revision: int | None = None
    recipe_share: RecipeShare | None = None
    nutrients: tuple[MealNutrientSnapshot, ...]


class MealSnapshot(FrozenModel):
    id: int
    revision_id: int
    revision_number: int
    label: str
    local_date: date
    timezone: str
    consumed_at: float
    deleted: bool
    operation: Operation
    items: tuple[MealItemSnapshot, ...]


def _validate_fields(label: str, local_date: date, timezone: str, consumed_at: float) -> str:
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
        raise MealError("Use a meal name between 1 and 120 characters.")
    if type(local_date) is not date:
        raise MealError("Choose a valid calendar date for this meal.")
    try:
        zone = ZoneInfo(timezone)
        if isinstance(consumed_at, bool) or not isinstance(consumed_at, int | float):
            raise ValueError
        if not math.isfinite(consumed_at):
            raise ValueError
        if datetime.fromtimestamp(consumed_at, zone).date() != local_date:
            raise MealError("The meal time and local date disagree; choose the date again.")
    except (TypeError, ValueError, OverflowError, OSError, ZoneInfoNotFoundError) as error:
        if isinstance(error, MealError):
            raise
        raise MealError("Choose a valid meal time and time zone.") from None
    return label.strip()


def _validate_item(item: MealItemInput) -> None:
    if item.recipe_share is not None:
        try:
            RecipeShare.model_validate(item.recipe_share.model_dump())
        except (AttributeError, ValidationError):
            raise MealError("Choose a valid recipe portion before logging it.") from None
    if type(item.food_version_id) is not int or not 1 <= item.food_version_id <= MAX_INTEGER:
        raise MealError("Choose a saved food version before logging it.")
    if type(item.edible_milligrams) is not int or not 1 <= item.edible_milligrams <= 50_000_000:
        raise MealError("Enter a measured edible mass greater than zero and at most 50 kg.")
    if not isinstance(item.original_quantity, str) or len(item.original_quantity) > 40:
        raise MealError("Enter the measured quantity as a decimal number.")
    factors = {"g": 1000, "kg": 1_000_000, "mg": 1}
    if not isinstance(item.original_unit, str) or item.original_unit.casefold() not in factors:
        raise MealError("Enter the measured edible amount in g, kg or mg.")
    try:
        with localcontext() as context:
            context.prec = 50
            value = exact_decimal(item.original_quantity) * factors[item.original_unit.casefold()]
        if value != item.edible_milligrams:
            raise ValueError
    except (ValueError, ArithmeticError):
        raise MealError(
            "The original quantity must match the measured edible mass exactly."
        ) from None
    provenance = (
        item.quantity_basis,
        item.approval_action_key,
        item.approved_at,
        item.approval_draft_id,
        item.approval_draft_revision,
    )
    if item.quantity_method == "measured":
        if any(value is not None for value in provenance):
            raise MealError("A measured amount cannot retain estimate-approval metadata.")
    elif item.quantity_method == "approved_estimate":
        if (
            not isinstance(item.quantity_basis, str)
            or not 1 <= len(item.quantity_basis) <= 300
            or not item.quantity_basis.strip()
            or not isinstance(item.approval_action_key, str)
            or not item.approval_action_key.startswith("callback:")
            or len(item.approval_action_key) <= 9
            or isinstance(item.approved_at, bool)
            or not isinstance(item.approved_at, int | float)
            or not 0 < item.approved_at <= 1.7976931348623157e308
            or type(item.approval_draft_id) is not int
            or not 1 <= item.approval_draft_id <= MAX_INTEGER
            or type(item.approval_draft_revision) is not int
            or not 1 <= item.approval_draft_revision <= MAX_INTEGER
        ):
            raise MealError("Approve the displayed estimate with its Telegram button first.")
    else:
        raise MealError("Choose a measured amount or explicitly approve the displayed estimate.")


def input_from_snapshot(item: MealItemSnapshot) -> MealItemInput:
    """Carry the exact quantity and approval provenance into an unrelated correction."""
    return MealItemInput(
        food_version_id=item.food_version_id,
        edible_milligrams=item.edible_milligrams,
        original_quantity=item.original_quantity,
        original_unit=item.original_unit,
        quantity_method=item.quantity_method,
        quantity_basis=item.quantity_basis,
        approval_action_key=item.approval_action_key,
        approved_at=item.approved_at,
        approval_draft_id=item.approval_draft_id,
        approval_draft_revision=item.approval_draft_revision,
        recipe_share=item.recipe_share,
    )


def _consumed_amount(
    amount_scaled: int | None,
    edible_milligrams: int,
    recipe_share: RecipeShare | None = None,
) -> int | None:
    if amount_scaled is None:
        return None
    numerator = amount_scaled * edible_milligrams
    denominator = 100_000
    if recipe_share is not None:
        numerator *= recipe_share.portion_units
        denominator *= recipe_share.total_units
    # Integer division gives exact half-even rounding, even for repeating shares.
    amount, remainder = divmod(numerator, denominator)
    if remainder * 2 > denominator or (remainder * 2 == denominator and amount % 2):
        amount += 1
    if not 0 <= amount <= MAX_INTEGER:
        raise MealError("This portion exceeds the supported nutrient range; check the food data.")
    return amount


async def _snapshot_items(
    connection: AsyncConnection,
    items: tuple[MealItemInput, ...],
    *,
    action_key: str,
    previous: tuple[MealItemSnapshot, ...] = (),
) -> tuple[MealItemSnapshot, ...]:
    if not 1 <= len(items) <= 10:
        raise MealError("Log between 1 and 10 foods in one meal.")
    result = []
    # Consume existing occurrences once: copying an item must not authorize adding another.
    unchanged = list(previous)
    for item in items:
        _validate_item(item)
        if item.recipe_share is not None:
            from nutrition_bot.adapters.database.recipes import validate_recipe_item
            from nutrition_bot.domain.drafts import PlannedItem

            try:
                await validate_recipe_item(
                    connection,
                    PlannedItem(
                        food_version_id=item.food_version_id,
                        edible_milligrams=item.edible_milligrams,
                        original_quantity=item.original_quantity,
                        original_unit=item.original_unit,
                        estimate_basis=item.quantity_basis,
                        recipe_share=item.recipe_share,
                    ),
                )
            except (RecipeError, ValidationError):
                raise MealError(
                    "This recipe portion has changed or its uncertainty needs fresh approval. "
                    "Select the recipe version again."
                ) from None
        if item.quantity_method == "approved_estimate" and item.approval_action_key != action_key:
            match = next(
                (index for index, old in enumerate(unchanged) if input_from_snapshot(old) == item),
                None,
            )
            if match is None:
                raise MealError("This changed estimate needs fresh approval from its draft button.")
            result.append(unchanged.pop(match))
            continue
        try:
            food = await get_food_version(connection, item.food_version_id)
        except ValueError:
            raise MealError(
                "That saved food version is unavailable; choose the food again."
            ) from None
        if food.record.preparation == "unspecified":
            raise MealError("Choose food data with a clear raw, cooked or packaged preparation.")
        result.append(
            MealItemSnapshot(
                food_version_id=food.version_id,
                food_name=food.record.name,
                preparation=food.record.preparation,
                source_kind=food.source_kind,
                source_reference=food.record.source_reference,
                source_url=food.record.source_url,
                source_license=food.record.source_license,
                food_content_sha256=food.content_sha256,
                calculation_version=(
                    f"recipe-consumed-rational-micro-v1/{food.calculation_version}"
                    if item.recipe_share
                    else f"meal-consumed-micro-v1/{food.calculation_version}"
                ),
                provenance=food.provenance,
                edible_milligrams=item.edible_milligrams,
                original_quantity=item.original_quantity,
                original_unit=item.original_unit,
                quantity_method=item.quantity_method,
                quantity_basis=item.quantity_basis,
                approval_action_key=item.approval_action_key,
                approved_at=item.approved_at,
                approval_draft_id=item.approval_draft_id,
                approval_draft_revision=item.approval_draft_revision,
                recipe_share=item.recipe_share,
                nutrients=tuple(
                    MealNutrientSnapshot(
                        code=nutrient.code,
                        unit=nutrient.unit,
                        amount_scaled=_consumed_amount(
                            nutrient.amount_scaled, item.edible_milligrams, item.recipe_share
                        ),
                        quality=nutrient.quality,
                    )
                    for nutrient in food.nutrients
                ),
            )
        )
    return tuple(result)


async def _validate_action(connection: AsyncConnection, action_key: str) -> None:
    if (
        await connection.scalar(
            sa.select(meal_revisions.c.id).where(meal_revisions.c.action_key == action_key)
        )
        is not None
    ):
        raise MealError("This action was already applied; open the latest meal receipt.")
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise MealError("This action is unavailable; open the meal again.")


async def _get_revision(connection: AsyncConnection, revision_id: int) -> MealSnapshot:
    revision = (
        (
            await connection.execute(
                sa.select(meal_revisions).where(
                    meal_revisions.c.id == revision_id, meal_revisions.c.sealed.is_(True)
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if revision is None:
        raise MealError("This meal is unavailable; open your meal list again.")
    item_rows = (
        (
            await connection.execute(
                sa.select(meal_items)
                .where(meal_items.c.revision_id == revision_id)
                .order_by(meal_items.c.item_index)
            )
        )
        .mappings()
        .all()
    )
    nutrient_rows = (
        (
            await connection.execute(
                sa.select(meal_item_nutrients)
                .where(meal_item_nutrients.c.revision_id == revision_id)
                .order_by(meal_item_nutrients.c.item_index, meal_item_nutrients.c.nutrient_code)
            )
        )
        .mappings()
        .all()
    )
    snapshots = tuple(
        MealItemSnapshot.model_validate(
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"revision_id", "item_index", "recipe_version_id"}
                },
                "nutrients": tuple(
                    MealNutrientSnapshot(
                        code=value["nutrient_code"],
                        unit=value["unit"],
                        amount_scaled=value["amount_scaled"],
                        quality=value["quality"],
                    )
                    for value in nutrient_rows
                    if value["item_index"] == row["item_index"]
                ),
            }
        )
        for row in item_rows
    )
    return MealSnapshot(
        id=revision["meal_id"],
        revision_id=revision["id"],
        revision_number=revision["revision_number"],
        label=revision["label"],
        local_date=revision["local_date"],
        timezone=revision["timezone"],
        consumed_at=revision["consumed_at"],
        deleted=revision["deleted"],
        operation=revision["operation"],
        items=snapshots,
    )


async def get_meal(connection: AsyncConnection, meal_id: int) -> MealSnapshot:
    if type(meal_id) is not int or not 1 <= meal_id <= MAX_INTEGER:
        raise MealError("Choose a valid meal from your meal list.")
    revision_id = await connection.scalar(
        sa.select(meals.c.current_revision_id).where(meals.c.id == meal_id)
    )
    if revision_id is None:
        raise MealError("This meal is unavailable; open your meal list again.")
    return await _get_revision(connection, revision_id)


async def _publish(
    connection: AsyncConnection,
    *,
    meal_id: int,
    current: MealSnapshot | None,
    action_key: str,
    label: str,
    local_date: date,
    timezone: str,
    consumed_at: float,
    deleted: bool,
    operation: Operation,
    items: tuple[MealItemSnapshot, ...],
) -> MealSnapshot:
    revision_id = (
        await connection.execute(
            sa.insert(meal_revisions)
            .values(
                meal_id=meal_id,
                revision_number=current.revision_number + 1 if current else 1,
                previous_revision_id=current.revision_id if current else None,
                action_key=action_key,
                label=label,
                local_date=local_date,
                timezone=timezone,
                consumed_at=consumed_at,
                deleted=deleted,
                operation=operation,
                sealed=False,
            )
            .returning(meal_revisions.c.id)
        )
    ).scalar_one()
    for index, item in enumerate(items):
        await connection.execute(
            sa.insert(meal_items).values(
                revision_id=revision_id,
                item_index=index,
                recipe_version_id=item.recipe_share.version_id if item.recipe_share else None,
                **item.model_dump(mode="json", exclude={"nutrients"}),
            )
        )
        for nutrient in item.nutrients:
            await connection.execute(
                sa.insert(meal_item_nutrients).values(
                    revision_id=revision_id,
                    item_index=index,
                    nutrient_code=nutrient.code,
                    **nutrient.model_dump(exclude={"code"}),
                )
            )
    await connection.execute(
        sa.update(meal_revisions).where(meal_revisions.c.id == revision_id).values(sealed=True)
    )
    await connection.execute(
        sa.update(meals).where(meals.c.id == meal_id).values(current_revision_id=revision_id)
    )
    return await _get_revision(connection, revision_id)


async def create_meal(
    connection: AsyncConnection,
    *,
    items: tuple[MealItemInput, ...],
    label: str,
    local_date: date,
    timezone: str,
    consumed_at: float,
    action_key: str,
    source_chat_id: int,
    source_message_id: int,
) -> MealSnapshot:
    """Save measured amounts or estimates bound to this verified approval action."""
    label = _validate_fields(label, local_date, timezone, consumed_at)
    if (
        type(source_chat_id) is not int
        or not -MAX_INTEGER <= source_chat_id <= MAX_INTEGER
        or source_chat_id == 0
        or type(source_message_id) is not int
        or not 1 <= source_message_id <= MAX_INTEGER
    ):
        raise MealError("This message is unavailable; send the measured meal again.")
    await _validate_action(connection, action_key)
    if (
        await connection.scalar(
            sa.select(meals.c.id).where(
                meals.c.source_chat_id == source_chat_id,
                meals.c.source_message_id == source_message_id,
            )
        )
        is not None
    ):
        raise MealError("This message already has a meal; use its Edit button to correct it.")
    snapshots = await _snapshot_items(connection, items, action_key=action_key)
    meal_id = (
        await connection.execute(
            sa.insert(meals)
            .values(
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                created_at=time.time(),
                current_revision_id=None,
            )
            .returning(meals.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection,
        meal_id=meal_id,
        current=None,
        action_key=action_key,
        label=label,
        local_date=local_date,
        timezone=timezone,
        consumed_at=consumed_at,
        deleted=False,
        operation="create",
        items=snapshots,
    )


async def _current(
    connection: AsyncConnection, meal_id: int, expected_revision_id: int, action_key: str
) -> MealSnapshot:
    current = await get_meal(connection, meal_id)
    if type(expected_revision_id) is not int or current.revision_id != expected_revision_id:
        raise MealError("This meal changed since that receipt; open the latest version first.")
    await _validate_action(connection, action_key)
    return current


async def revise_meal(
    connection: AsyncConnection,
    meal_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
    items: tuple[MealItemInput, ...] | None = None,
    label: str | None = None,
    local_date: date | None = None,
    consumed_at: float | None = None,
    deleted: bool | None = None,
    operation: str = "edit",
) -> MealSnapshot:
    current = await _current(connection, meal_id, expected_revision_id, action_key)
    if operation not in {"edit", "delete"} or (deleted is not None and type(deleted) is not bool):
        raise MealError("Choose Edit or Delete from the latest meal receipt.")
    chosen_date = current.local_date if local_date is None else local_date
    chosen_time = current.consumed_at if consumed_at is None else consumed_at
    if local_date is not None and consumed_at is None:
        if type(local_date) is not date:
            raise MealError("Choose a valid calendar date for this meal.")
        local_time = datetime.fromtimestamp(current.consumed_at, ZoneInfo(current.timezone))
        chosen_time = datetime.combine(local_date, local_time.timetz()).timestamp()
    chosen_label = _validate_fields(
        current.label if label is None else label, chosen_date, current.timezone, chosen_time
    )
    chosen_deleted = current.deleted if deleted is None else deleted
    if operation == "delete":
        if current.deleted:
            raise MealError("This meal is already deleted; use Undo on its latest receipt.")
        chosen_deleted = True
    snapshots = (
        current.items
        if items is None
        else await _snapshot_items(connection, items, action_key=action_key, previous=current.items)
    )
    return await _publish(
        connection,
        meal_id=meal_id,
        current=current,
        action_key=action_key,
        label=chosen_label,
        local_date=chosen_date,
        timezone=current.timezone,
        consumed_at=chosen_time,
        deleted=chosen_deleted,
        operation="delete" if operation == "delete" else "edit",
        items=snapshots,
    )


async def undo_meal(
    connection: AsyncConnection,
    meal_id: int,
    expected_revision_id: int,
    *,
    action_key: str,
) -> MealSnapshot:
    current = await _current(connection, meal_id, expected_revision_id, action_key)
    if current.operation == "undo":
        raise MealError("That change was already undone; use Edit for another correction.")
    previous_id = await connection.scalar(
        sa.select(meal_revisions.c.previous_revision_id).where(
            meal_revisions.c.id == current.revision_id
        )
    )
    previous = current if previous_id is None else await _get_revision(connection, previous_id)
    return await _publish(
        connection,
        meal_id=meal_id,
        current=current,
        action_key=action_key,
        label=previous.label,
        local_date=previous.local_date,
        timezone=previous.timezone,
        consumed_at=previous.consumed_at,
        deleted=True if previous_id is None else previous.deleted,
        operation="undo",
        items=previous.items,
    )
