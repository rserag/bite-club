"""Immutable recipe batches. Callers own authorization, actions and transactions."""

import time
from collections.abc import Sequence

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions, food_versions, foods
from nutrition_bot.adapters.database.schema_recipes import (
    recipe_ingredients,
    recipe_versions,
    recipes,
)
from nutrition_bot.domain.drafts import PlannedItem
from nutrition_bot.domain.food import MAX_INTEGER, milligrams_to_grams
from nutrition_bot.domain.recipes import (
    RecipeDefinition,
    RecipeError,
    RecipeSnapshot,
    normalize_recipe_name,
    recipe_display_name,
    recipe_portions,
)


def _identifier(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise RecipeError("Choose a saved recipe from /recipes.")


async def _action(connection: AsyncConnection, key: str) -> None:
    if (
        not isinstance(key, str)
        or not key
        or await connection.scalar(sa.select(actions.c.key).where(actions.c.key == key)) is None
    ):
        raise RecipeError("This action is unavailable. Open the recipe again.")
    if (
        await connection.scalar(
            sa.select(recipe_versions.c.id).where(recipe_versions.c.action_key == key)
        )
        is not None
    ):
        raise RecipeError("This action was already applied. Open the current recipe.")


async def _definition(connection: AsyncConnection, value: RecipeDefinition) -> RecipeDefinition:
    try:
        value = RecipeDefinition.model_validate(value.model_dump())
    except ValidationError:
        raise RecipeError(
            "Supply one to ten resolved ingredients and a valid batch yield or serving definition."
        ) from None
    identities = {item.food_version_id for item in value.items}
    found: set[int] = set(
        (
            await connection.scalars(
                sa.select(food_versions.c.id)
                .join(foods, foods.c.id == food_versions.c.food_id)
                .where(
                    food_versions.c.id.in_(identities),
                    food_versions.c.sealed.is_(True),
                    foods.c.preparation != "unspecified",
                )
            )
        ).all()
    )
    if found != identities:
        raise RecipeError(
            "Choose reviewed ingredient versions with clear raw, cooked or packaged preparation."
        )
    return value


async def get_recipe_version(connection: AsyncConnection, version_id: int) -> RecipeSnapshot:
    _identifier(version_id)
    row = (
        (
            await connection.execute(
                sa.select(recipes.c.name, recipe_versions)
                .join(recipe_versions, recipe_versions.c.recipe_id == recipes.c.id)
                .where(recipe_versions.c.id == version_id, recipe_versions.c.sealed.is_(True))
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RecipeError("This recipe version is unavailable. Choose one from /recipes.")
    ingredients = (
        (
            await connection.execute(
                sa.select(recipe_ingredients)
                .where(recipe_ingredients.c.version_id == version_id)
                .order_by(recipe_ingredients.c.item_index)
            )
        )
        .mappings()
        .all()
    )
    return RecipeSnapshot(
        id=row["recipe_id"],
        version_id=version_id,
        version_number=row["version_number"],
        name=row["name"],
        archived=row["archived"],
        definition=RecipeDefinition(
            unit=row["unit"],
            total_units=row["total_units"],
            estimate_basis=row["estimate_basis"],
            items=tuple(
                PlannedItem(
                    food_version_id=item["food_version_id"],
                    edible_milligrams=item["edible_milligrams"],
                    original_quantity=format(milligrams_to_grams(item["edible_milligrams"]), "f"),
                    original_unit="g",
                    estimate_basis=item["estimate_basis"],
                )
                for item in ingredients
            ),
        ),
    )


async def get_recipe(connection: AsyncConnection, recipe_id: int) -> RecipeSnapshot | None:
    _identifier(recipe_id)
    version_id = await connection.scalar(
        sa.select(recipes.c.current_version_id).where(recipes.c.id == recipe_id)
    )
    return await get_recipe_version(connection, version_id) if version_id is not None else None


async def get_recipe_by_number(
    connection: AsyncConnection, recipe_id: int, version_number: int
) -> RecipeSnapshot | None:
    _identifier(recipe_id)
    _identifier(version_number)
    version_id = await connection.scalar(
        sa.select(recipe_versions.c.id).where(
            recipe_versions.c.recipe_id == recipe_id,
            recipe_versions.c.version_number == version_number,
            recipe_versions.c.sealed.is_(True),
        )
    )
    return await get_recipe_version(connection, version_id) if version_id is not None else None


async def list_recipes(
    connection: AsyncConnection, *, limit: int = 10, include_archived: bool = False
) -> tuple[RecipeSnapshot, ...]:
    if type(limit) is not int or not 1 <= limit <= 50 or type(include_archived) is not bool:
        raise RecipeError("List between one and fifty recipes at a time.")
    statement = (
        sa.select(recipe_versions.c.id)
        .join(recipes, recipes.c.current_version_id == recipe_versions.c.id)
        .where(recipe_versions.c.sealed.is_(True))
    )
    if not include_archived:
        statement = statement.where(recipe_versions.c.archived.is_(False))
    versions: Sequence[int] = (
        await connection.scalars(
            statement.order_by(recipes.c.normalized_name, recipes.c.id).limit(limit)
        )
    ).all()
    return tuple([await get_recipe_version(connection, value) for value in versions])


async def _publish(
    connection: AsyncConnection,
    recipe_id: int,
    definition: RecipeDefinition,
    *,
    previous: RecipeSnapshot | None,
    archived: bool,
    action_key: str,
) -> RecipeSnapshot:
    number = previous.version_number + 1 if previous else 1
    if number > MAX_INTEGER:
        raise RecipeError("This recipe cannot accept another version. Save a new recipe.")
    version_id = (
        await connection.execute(
            sa.insert(recipe_versions)
            .values(
                recipe_id=recipe_id,
                version_number=number,
                previous_version_id=previous.version_id if previous else None,
                action_key=action_key,
                unit=definition.unit,
                total_units=definition.total_units,
                estimate_basis=definition.estimate_basis,
                archived=archived,
                sealed=False,
                created_at=time.time(),
            )
            .returning(recipe_versions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(recipe_ingredients),
        [
            dict(
                version_id=version_id,
                item_index=index,
                food_version_id=item.food_version_id,
                edible_milligrams=item.edible_milligrams,
                estimate_basis=item.estimate_basis,
            )
            for index, item in enumerate(definition.items)
        ],
    )
    await connection.execute(
        sa.update(recipe_versions).where(recipe_versions.c.id == version_id).values(sealed=True)
    )
    await connection.execute(
        sa.update(recipes).where(recipes.c.id == recipe_id).values(current_version_id=version_id)
    )
    return await get_recipe_version(connection, version_id)


async def create_recipe(
    connection: AsyncConnection, name: str, definition: RecipeDefinition, *, action_key: str
) -> RecipeSnapshot:
    display, normalized = recipe_display_name(name), normalize_recipe_name(name)
    await _action(connection, action_key)
    definition = await _definition(connection, definition)
    if (
        await connection.scalar(
            sa.select(recipes.c.id).where(recipes.c.normalized_name == normalized)
        )
        is not None
    ):
        raise RecipeError(
            "That recipe name already exists. Update or restore it, or choose another name."
        )
    recipe_id = (
        await connection.execute(
            sa.insert(recipes)
            .values(name=display, normalized_name=normalized, created_at=time.time())
            .returning(recipes.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection, recipe_id, definition, previous=None, archived=False, action_key=action_key
    )


async def revise_recipe(
    connection: AsyncConnection,
    recipe_id: int,
    expected_version_id: int,
    *,
    action_key: str,
    definition: RecipeDefinition | None = None,
    archived: bool | None = None,
) -> RecipeSnapshot:
    _identifier(expected_version_id)
    current = await get_recipe(connection, recipe_id)
    if current is None:
        raise RecipeError("This recipe is unavailable. Choose one from /recipes.")
    if current.version_id != expected_version_id:
        raise RecipeError("This recipe changed. Open its current version before editing it.")
    if (archived is not None and type(archived) is not bool) or (
        archived is None and definition is None
    ):
        raise RecipeError("Choose a new definition, Archive or Restore.")
    await _action(connection, action_key)
    content = (
        await _definition(connection, definition) if definition is not None else current.definition
    )
    return await _publish(
        connection,
        recipe_id,
        content,
        previous=current,
        archived=current.archived if archived is None else archived,
        action_key=action_key,
    )


async def validate_recipe_item(connection: AsyncConnection, item: PlannedItem) -> None:
    share = item.recipe_share
    if share is None:
        return
    try:
        item = PlannedItem.model_validate(item.model_dump())
        share = item.recipe_share
        assert share is not None
        recipe = await get_recipe_version(connection, share.version_id)
        expected = recipe_portions(
            recipe, share.portion_units, estimate_basis=share.portion_estimate_basis
        )
        if share.ingredient_index >= len(expected) or item != expected[share.ingredient_index]:
            raise RecipeError(
                "This recipe portion changed or has inconsistent source data. "
                "Select the recipe version again."
            )
    except ValidationError:
        raise RecipeError(
            "This recipe portion has invalid source data. Select the recipe version again."
        ) from None
