"""Versioned exact-portion favorites. Callers own write transactions and action rows."""

import time

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.meals import MealError, MealSnapshot, get_meal
from nutrition_bot.adapters.database.schema import actions
from nutrition_bot.adapters.database.schema_favorites import (
    favorite_items,
    favorite_versions,
    favorites,
)
from nutrition_bot.domain.drafts import PlannedItem
from nutrition_bot.domain.food import MAX_INTEGER, milligrams_to_grams
from nutrition_bot.domain.reuse import (
    FavoriteSnapshot,
    ReuseError,
    favorite_display_name,
    normalize_favorite_name,
    planned_from_meal,
)


def _identifier(value: int) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ReuseError("Choose a saved favorite from /favorites.")


async def _validate_action(connection: AsyncConnection, action_key: str) -> None:
    if not isinstance(action_key, str) or not action_key:
        raise ReuseError("This action is unavailable. Open the favorite again.")
    if await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key)) is None:
        raise ReuseError("This action is unavailable. Open the favorite again.")
    if (
        await connection.scalar(
            sa.select(favorite_versions.c.id).where(favorite_versions.c.action_key == action_key)
        )
        is not None
    ):
        raise ReuseError("This action was already applied. Open the favorite again.")


async def _current_meal(connection: AsyncConnection, meal: MealSnapshot) -> MealSnapshot:
    try:
        current = await get_meal(connection, meal.id)
    except MealError:
        raise ReuseError("Choose a saved meal before creating a favorite.") from None
    if current.revision_id != meal.revision_id:
        raise ReuseError("That meal changed. Open its current receipt before saving a favorite.")
    if current.deleted:
        raise ReuseError("A deleted meal cannot become a favorite. Restore or choose another meal.")
    return current


async def _version(
    connection: AsyncConnection, favorite_id: int, version_id: int
) -> FavoriteSnapshot:
    row = (
        (
            await connection.execute(
                sa.select(favorites.c.name, favorite_versions)
                .join(favorite_versions, favorite_versions.c.favorite_id == favorites.c.id)
                .where(
                    favorites.c.id == favorite_id,
                    favorite_versions.c.id == version_id,
                    favorite_versions.c.sealed.is_(True),
                )
            )
        )
        .mappings()
        .one()
    )
    item_rows = (
        (
            await connection.execute(
                sa.select(favorite_items)
                .where(favorite_items.c.version_id == version_id)
                .order_by(favorite_items.c.item_index)
            )
        )
        .mappings()
        .all()
    )
    return FavoriteSnapshot(
        id=favorite_id,
        version_id=version_id,
        version_number=row["version_number"],
        name=row["name"],
        archived=row["archived"],
        items=tuple(
            PlannedItem(
                recipe_share=item["recipe_share"],
                food_version_id=item["food_version_id"],
                edible_milligrams=item["edible_milligrams"],
                original_quantity=format(milligrams_to_grams(item["edible_milligrams"]), "f"),
                original_unit="g",
                estimate_basis=item["estimate_basis"],
            )
            for item in item_rows
        ),
    )


async def get_favorite(connection: AsyncConnection, favorite_id: int) -> FavoriteSnapshot | None:
    _identifier(favorite_id)
    current = await connection.scalar(
        sa.select(favorites.c.current_version_id).where(favorites.c.id == favorite_id)
    )
    return await _version(connection, favorite_id, current) if current is not None else None


async def find_favorite(connection: AsyncConnection, name: str) -> FavoriteSnapshot | None:
    favorite_id = await connection.scalar(
        sa.select(favorites.c.id).where(
            favorites.c.normalized_name == normalize_favorite_name(name)
        )
    )
    found = await get_favorite(connection, favorite_id) if favorite_id is not None else None
    return found if found is not None and not found.archived else None


async def list_favorites(
    connection: AsyncConnection, *, limit: int = 10, include_archived: bool = False
) -> tuple[FavoriteSnapshot, ...]:
    if type(limit) is not int or not 1 <= limit <= 50 or type(include_archived) is not bool:
        raise ReuseError("List between 1 and 50 favorites at a time.")
    statement = (
        sa.select(favorites.c.id, favorites.c.current_version_id)
        .join(favorite_versions, favorite_versions.c.id == favorites.c.current_version_id)
        .where(favorite_versions.c.sealed.is_(True))
    )
    if not include_archived:
        statement = statement.where(favorite_versions.c.archived.is_(False))
    rows = (
        await connection.execute(
            statement.order_by(favorites.c.normalized_name, favorites.c.id).limit(limit)
        )
    ).all()
    return tuple([await _version(connection, row.id, row.current_version_id) for row in rows])


async def _publish(
    connection: AsyncConnection,
    *,
    favorite_id: int,
    previous: FavoriteSnapshot | None,
    items: tuple[PlannedItem, ...],
    archived: bool,
    action_key: str,
) -> FavoriteSnapshot:
    from nutrition_bot.adapters.database.recipes import validate_recipe_item

    for item in items:
        if item.recipe_share is not None:
            await validate_recipe_item(connection, item)
    version_number = previous.version_number + 1 if previous else 1
    if version_number > MAX_INTEGER:
        raise ReuseError("This favorite cannot accept another version. Save a new favorite.")
    version_id = (
        await connection.execute(
            sa.insert(favorite_versions)
            .values(
                favorite_id=favorite_id,
                version_number=version_number,
                previous_version_id=previous.version_id if previous else None,
                action_key=action_key,
                archived=archived,
                sealed=False,
                created_at=time.time(),
            )
            .returning(favorite_versions.c.id)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(favorite_items),
        [
            dict(
                version_id=version_id,
                item_index=index,
                food_version_id=item.food_version_id,
                edible_milligrams=item.edible_milligrams,
                estimate_basis=item.estimate_basis,
                recipe_share=item.recipe_share.model_dump(mode="json")
                if item.recipe_share
                else None,
                recipe_version_id=item.recipe_share.version_id if item.recipe_share else None,
            )
            for index, item in enumerate(items)
        ],
    )
    await connection.execute(
        sa.update(favorite_versions).where(favorite_versions.c.id == version_id).values(sealed=True)
    )
    await connection.execute(
        sa.update(favorites)
        .where(favorites.c.id == favorite_id)
        .values(current_version_id=version_id)
    )
    return await _version(connection, favorite_id, version_id)


async def create_favorite(
    connection: AsyncConnection, name: str, meal: MealSnapshot, *, action_key: str
) -> FavoriteSnapshot:
    display, normalized = favorite_display_name(name), normalize_favorite_name(name)
    await _validate_action(connection, action_key)
    if (
        await connection.scalar(
            sa.select(favorites.c.id).where(favorites.c.normalized_name == normalized)
        )
        is not None
    ):
        raise ReuseError(
            "That favorite name already exists. Update or restore it, or choose another name."
        )
    current = await _current_meal(connection, meal)
    items = planned_from_meal(current)
    favorite_id = (
        await connection.execute(
            sa.insert(favorites)
            .values(name=display, normalized_name=normalized, created_at=time.time())
            .returning(favorites.c.id)
        )
    ).scalar_one()
    return await _publish(
        connection,
        favorite_id=favorite_id,
        previous=None,
        items=items,
        archived=False,
        action_key=action_key,
    )


async def revise_favorite(
    connection: AsyncConnection,
    favorite_id: int,
    expected_version_id: int,
    *,
    action_key: str,
    meal: MealSnapshot | None = None,
    archived: bool | None = None,
) -> FavoriteSnapshot:
    _identifier(expected_version_id)
    current = await get_favorite(connection, favorite_id)
    if current is None:
        raise ReuseError("This favorite is unavailable. Choose one from /favorites.")
    if current.version_id != expected_version_id:
        raise ReuseError("That favorite changed. Open its current version before editing it.")
    if archived is not None and type(archived) is not bool:
        raise ReuseError("Choose Archive or Restore for this favorite.")
    if meal is None and archived is None:
        raise ReuseError("Choose a replacement meal, Archive, or Restore.")
    await _validate_action(connection, action_key)
    items = planned_from_meal(await _current_meal(connection, meal)) if meal else current.items
    return await _publish(
        connection,
        favorite_id=favorite_id,
        previous=current,
        items=items,
        archived=current.archived if archived is None else archived,
        action_key=action_key,
    )
