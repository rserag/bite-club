"""Explicit, revision-checked aliases. Callers own transactions and authorization."""

import time
import unicodedata

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from nutrition_bot.adapters.database.schema import actions, food_versions, foods
from nutrition_bot.adapters.database.schema_aliases import food_aliases
from nutrition_bot.domain.aliases import Alias, AliasError, alias_display_name, normalize_alias
from nutrition_bot.domain.food import MAX_INTEGER


def _snapshot(row: RowMapping) -> Alias:
    return Alias(
        id=row["id"],
        revision=row["revision"],
        name=row["name"],
        food_version_id=row["food_version_id"],
        active=bool(row["active"]),
    )


def _valid_id(value: int) -> bool:
    return type(value) is int and 1 <= value <= MAX_INTEGER


async def _validate_action(connection: AsyncConnection, action_key: str) -> None:
    if (
        not isinstance(action_key, str)
        or not action_key
        or await connection.scalar(sa.select(actions.c.key).where(actions.c.key == action_key))
        is None
    ):
        raise AliasError("This action is unavailable; open the alias controls again.")
    if (
        await connection.scalar(
            sa.select(food_aliases.c.id).where(
                sa.or_(
                    food_aliases.c.created_action_key == action_key,
                    food_aliases.c.updated_action_key == action_key,
                )
            )
        )
        is not None
    ):
        raise AliasError("This action was already applied; open the latest alias controls.")


async def _validate_target(connection: AsyncConnection, food_version_id: int) -> None:
    if (
        not _valid_id(food_version_id)
        or await connection.scalar(
            sa.select(food_versions.c.id)
            .join(foods, foods.c.id == food_versions.c.food_id)
            .where(
                food_versions.c.id == food_version_id,
                food_versions.c.sealed.is_(True),
                foods.c.preparation != "unspecified",
            )
        )
        is None
    ):
        raise AliasError("Choose a reviewed food version with a clear preparation first.")


async def create_alias(
    connection: AsyncConnection, name: str, food_version_id: int, *, action_key: str
) -> Alias:
    display, canonical = alias_display_name(name), normalize_alias(name)
    if (
        await connection.scalar(
            sa.select(food_aliases.c.id).where(food_aliases.c.normalized_name == canonical)
        )
        is not None
    ):
        raise AliasError("That alias already exists; update or reactivate its existing entry.")
    await _validate_target(connection, food_version_id)
    await _validate_action(connection, action_key)
    now = time.time()
    row = (
        (
            await connection.execute(
                sa.insert(food_aliases)
                .values(
                    revision=1,
                    name=display,
                    normalized_name=canonical,
                    food_version_id=food_version_id,
                    active=True,
                    created_action_key=action_key,
                    updated_action_key=action_key,
                    created_at=now,
                    updated_at=now,
                )
                .returning(food_aliases)
            )
        )
        .mappings()
        .one()
    )
    return _snapshot(row)


async def get_alias(connection: AsyncConnection, alias_id: int) -> Alias | None:
    if not _valid_id(alias_id):
        return None
    row = (
        (await connection.execute(sa.select(food_aliases).where(food_aliases.c.id == alias_id)))
        .mappings()
        .one_or_none()
    )
    return None if row is None else _snapshot(row)


async def resolve_alias(connection: AsyncConnection, name: str) -> Alias | None:
    try:
        canonical = normalize_alias(name)
    except AliasError:
        return None
    row = (
        (
            await connection.execute(
                sa.select(food_aliases).where(
                    food_aliases.c.normalized_name == canonical, food_aliases.c.active.is_(True)
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _snapshot(row)


async def list_aliases(
    connection: AsyncConnection, *, query: str = "", limit: int = 10
) -> tuple[Alias, ...]:
    """List active and disabled aliases; search is a literal canonical substring."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise AliasError("Choose a list size between 1 and 50.")
    if (
        not isinstance(query, str)
        or len(query) > 240
        or any(unicodedata.category(character).startswith("C") for character in query)
    ):
        raise AliasError("Use a short readable alias search.")
    canonical = unicodedata.normalize(
        "NFC", " ".join(unicodedata.normalize("NFC", query).casefold().split())
    )
    rows = (
        (
            await connection.execute(
                sa.select(food_aliases)
                .where(food_aliases.c.normalized_name.contains(canonical, autoescape=True))
                .order_by(food_aliases.c.normalized_name, food_aliases.c.id)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    return tuple(_snapshot(row) for row in rows)


async def update_alias(
    connection: AsyncConnection,
    alias_id: int,
    expected_revision: int,
    *,
    food_version_id: int | None = None,
    active: bool | None = None,
    action_key: str,
) -> Alias:
    current = await get_alias(connection, alias_id)
    if current is None:
        raise AliasError("This alias is unavailable; open your alias list again.")
    if not _valid_id(expected_revision) or current.revision != expected_revision:
        raise AliasError("This alias changed; open its latest controls before updating it.")
    if current.revision >= MAX_INTEGER:
        raise AliasError("This alias cannot accept another revision.")
    if food_version_id is None and active is None:
        raise AliasError("Choose a replacement food or change whether the alias is active.")
    if active is not None and type(active) is not bool:
        raise AliasError("Choose whether the alias is active.")
    target = current.food_version_id if food_version_id is None else food_version_id
    await _validate_target(connection, target)
    await _validate_action(connection, action_key)
    row = (
        (
            await connection.execute(
                sa.update(food_aliases)
                .where(food_aliases.c.id == alias_id, food_aliases.c.revision == expected_revision)
                .values(
                    revision=expected_revision + 1,
                    food_version_id=target,
                    active=current.active if active is None else active,
                    updated_action_key=action_key,
                    updated_at=time.time(),
                )
                .returning(food_aliases)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AliasError("This alias changed; open its latest controls before updating it.")
    return _snapshot(row)
