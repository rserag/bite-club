import asyncio
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database.aliases import (
    create_alias,
    get_alias,
    list_aliases,
    resolve_alias,
    update_alias,
)
from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
from nutrition_bot.adapters.database.meals import get_meal
from nutrition_bot.adapters.database.schema_aliases import food_aliases
from nutrition_bot.adapters.database.schema_drafts import meal_drafts
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.domain.aliases import AliasError, normalize_alias
from tests.test_food_storage import insert_unsealed_version, reviewed_food
from tests.test_meal_ledger import WHEN, action, create


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


async def alias(connection, food, name="Usual rice", key="alias-create"):
    await action(connection, key)
    return await create_alias(connection, name, food.version_id, action_key=key)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("  My  Rice  ", "my rice"),
        ("Cafe\u0301 cheese", "café cheese"),
        ("CAFÉ CHEESE", "café cheese"),
        ("Straße", "strasse"),
        ("My\u00a0\u00a0Rice", "my rice"),
        ("ՀԱՎԻ ՄԻՍ", "հավի միս"),
        ("Milk 2%", "milk 2%"),
        ("VitaminD3 powder", "vitamind3 powder"),
    ],
)
def test_alias_normalization_is_unicode_aware_and_deterministic(name, expected):
    assert normalize_alias(name) == expected
    assert normalize_alias(expected) == expected


@pytest.mark.parametrize(
    "name",
    [
        None,
        123,
        "",
        " ",
        "x" * 81,
        "rice\n",
        "rice\t",
        "rice\x00",
        "rice\u202e",
        "rice\u2028oil",
        "rice\u2029oil",
        "#12",
        "#0",
        "/meal",
        "/rice",
        "I ate rice",
        "I ate",
        "rice and beans",
        "rice; beans",
        "Dinner: rice",
        "about rice",
        "roughly rice",
        "around rice",
        "approximately rice",
        "estimated rice",
        "~rice",
        "≈rice",
        "rice ≅",
        "today rice",
        "yesterday rice",
        "tomorrow rice",
        "last rice",
        "Monday rice",
        "2026-09-21",
        "rice 2026-09-21",
        "3 eggs",
        "three eggs",
        "two eggs",
        "a banana",
        "１２３ eggs",
        "1/2 rice",
        "1,5 rice",
        "150g rice",
        "rice 150g",
        "rice 2",
        "150ish rice",
        "rice g",
        "kg rice",
        "rice mg",
        "rice grams",
        "rice oz",
        "rice lb",
        "rice tbsp",
        "rice cup",
        "milk ml",
        "rice plus oil",
        "rice or oil",
        "rice?",
    ],
)
def test_unreachable_or_ambiguous_alias_names_are_rejected_with_fixed_help(name):
    with pytest.raises(AliasError, match="1–80 readable characters"):
        normalize_alias(name)


async def test_creation_retains_display_name_and_resolves_canonical_name(store, food):
    async with store.write() as connection:
        saved = await alias(connection, food, "  Cafe\u0301   Rice  ")
        assert saved.name == "Café   Rice"
        assert saved.revision == 1 and saved.active
        assert saved.food_version_id == food.version_id
        assert await resolve_alias(connection, "CAFÉ rice") == saved
        assert await get_alias(connection, saved.id) == saved
        with pytest.raises(FrozenInstanceError):
            saved.revision = 99


@pytest.mark.parametrize(
    "first,second",
    [("Café cheese", "Cafe\u0301 CHEESE"), ("My  Rice", "my rice"), ("Straße", "STRASSE")],
)
async def test_canonical_collisions_never_silently_replace_aliases(store, food, first, second):
    async with store.write() as connection:
        saved = await alias(connection, food, first)
        await action(connection, "duplicate")
        with pytest.raises(AliasError, match="already exists"):
            await create_alias(connection, second, food.version_id, action_key="duplicate")
        assert await get_alias(connection, saved.id) == saved


async def test_inactive_alias_stays_reserved_and_explicit_reactivation_is_revisioned(store, food):
    async with store.write() as connection:
        saved = await alias(connection, food)
        await action(connection, "disable")
        disabled = await update_alias(
            connection, saved.id, saved.revision, active=False, action_key="disable"
        )
        assert disabled.revision == 2 and not disabled.active
        assert await resolve_alias(connection, saved.name) is None
        assert await list_aliases(connection) == (disabled,)
        await action(connection, "duplicate")
        with pytest.raises(AliasError, match="already exists"):
            await create_alias(connection, "USUAL RICE", food.version_id, action_key="duplicate")
        await action(connection, "stale")
        with pytest.raises(AliasError, match="changed"):
            await update_alias(connection, saved.id, 1, active=True, action_key="stale")
        await action(connection, "reactivate")
        active = await update_alias(
            connection, saved.id, disabled.revision, active=True, action_key="reactivate"
        )
        assert active.revision == 3 and active.active
        assert await resolve_alias(connection, saved.name) == active


async def test_alias_target_is_pinned_and_rebinding_cannot_change_saved_meal_snapshots(store, food):
    async with store.write() as connection:
        saved = await alias(connection, food)
        original_meal = await create(connection, food)
        original_food = await get_food_version(connection, food.version_id)
        newer = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic changed food"), food_id=food.food_id
        )
        assert (await resolve_alias(connection, saved.name)).food_version_id == food.version_id
        await action(connection, "rebind")
        rebound = await update_alias(
            connection,
            saved.id,
            saved.revision,
            food_version_id=newer.version_id,
            action_key="rebind",
        )
        assert rebound.food_version_id == newer.version_id and rebound.revision == 2
        assert await get_meal(connection, original_meal.id) == original_meal
        assert await get_food_version(connection, food.version_id) == original_food


async def test_target_requires_exact_valid_sealed_version_and_clear_preparation(store, food):
    async with store.write() as connection:
        unsealed = await insert_unsealed_version(connection, food.food_id)
        unclear = await publish_reviewed_food(connection, reviewed_food(preparation="unspecified"))
        await action(connection, "invalid-target")
        for target in (0, -1, True, "1", 2**63, 99999, unsealed, unclear.version_id):
            with pytest.raises(AliasError, match="reviewed food version"):
                await create_alias(connection, "Target alias", target, action_key="invalid-target")
        saved = await alias(connection, food)
        with pytest.raises(AliasError, match="reviewed food version"):
            await update_alias(
                connection,
                saved.id,
                saved.revision,
                food_version_id=unsealed,
                action_key="invalid-target",
            )
        assert await get_alias(connection, saved.id) == saved


async def test_invalid_ids_revisions_actions_and_updates_fail_safely(store, food):
    async with store.write() as connection:
        with pytest.raises(AliasError, match="action is unavailable"):
            await create_alias(connection, "Usual rice", food.version_id, action_key="missing")
        saved = await alias(connection, food)
        for invalid in (0, -1, True, "1", 2**63, 99999):
            assert await get_alias(connection, invalid) is None
            with pytest.raises(AliasError, match="unavailable"):
                await update_alias(connection, invalid, 1, active=False, action_key="missing")
        for revision in (0, -1, True, "1", 2**63, 99999):
            with pytest.raises(AliasError, match="changed"):
                await update_alias(
                    connection, saved.id, revision, active=False, action_key="missing"
                )
        with pytest.raises(AliasError, match="already applied"):
            await update_alias(
                connection, saved.id, saved.revision, active=False, action_key="alias-create"
            )
        with pytest.raises(AliasError, match="replacement food"):
            await update_alias(connection, saved.id, saved.revision, action_key="missing")
        with pytest.raises(AliasError, match="whether the alias is active"):
            await update_alias(connection, saved.id, saved.revision, active=1, action_key="missing")
        for name in ("#12", "today rice", "x" * 81, None):
            assert await resolve_alias(connection, name) is None


async def test_explicit_alias_may_equal_a_catalog_name_without_implicit_rebinding(store, food):
    async with store.write() as connection:
        target = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic other food", preparation="cooked")
        )
        saved = await alias(connection, target, food.record.name)
        assert (await resolve_alias(connection, food.record.name)) == saved
        assert saved.food_version_id == target.version_id


async def test_list_is_sorted_bounded_and_uses_literal_unicode_substring_search(store, food):
    async with store.write() as connection:
        for index, name in enumerate(("Zed rice", "Café rice", "Milk 2%", "Milk plain", "My_rice")):
            await alias(connection, food, name, key=f"alias-{index}")
        assert [item.name for item in await list_aliases(connection, limit=2)] == [
            "Café rice",
            "Milk 2%",
        ]
        assert [item.name for item in await list_aliases(connection, query="CAFE\u0301")] == [
            "Café rice"
        ]
        assert [item.name for item in await list_aliases(connection, query="%")] == ["Milk 2%"]
        assert [item.name for item in await list_aliases(connection, query="_")] == ["My_rice"]
        for limit in (0, 51, True, "10"):
            with pytest.raises(AliasError, match="between 1 and 50"):
                await list_aliases(connection, limit=limit)
        for query in (None, "x" * 241, "rice\n"):
            with pytest.raises(AliasError, match="short readable"):
                await list_aliases(connection, query=query)


async def test_alias_methods_do_not_commit_the_callers_transaction(store, food):
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        async with store.write() as connection:
            await alias(connection, food)
            raise RuntimeError("synthetic rollback")
    async with store.engine.connect() as connection:
        assert await list_aliases(connection) == ()


async def test_sql_cannot_delete_rename_or_replace_an_alias_identity(store, food):
    async with store.write() as connection:
        saved = await alias(connection, food)
        await action(connection, "replacement")
        values = dict((await connection.execute(sa.select(food_aliases))).mappings().one())
        for changes in (
            {},
            {"id": saved.id + 1},
            {"normalized_name": "other name"},
            {"id": saved.id + 1, "normalized_name": "other name"},
            {
                "id": saved.id + 1,
                "normalized_name": "other name",
                "created_action_key": "replacement",
            },
        ):
            with pytest.raises(IntegrityError, match="cannot be replaced"):
                await connection.execute(
                    sa.insert(food_aliases)
                    .prefix_with("OR REPLACE")
                    .values(**{**values, **changes})
                )
        with pytest.raises(IntegrityError, match="deactivate aliases"):
            await connection.execute(sa.delete(food_aliases).where(food_aliases.c.id == saved.id))
        for changes in (
            {"name": "Other display"},
            {"normalized_name": "other canonical"},
            {"id": saved.id + 1},
            {"revision": 4},
            {"active": False},
        ):
            with pytest.raises(IntegrityError, match="identity and revision"):
                await connection.execute(
                    sa.update(food_aliases).where(food_aliases.c.id == saved.id).values(**changes)
                )
        assert await get_alias(connection, saved.id) == saved


async def test_sql_target_guard_and_action_foreign_key_reject_invalid_bindings(store, food):
    async with store.write() as connection:
        saved = await alias(connection, food)
        unsealed = await insert_unsealed_version(connection, food.food_id)
        await action(connection, "target-change")
        with pytest.raises(IntegrityError, match="reviewed food version"):
            await connection.execute(
                sa.update(food_aliases)
                .where(food_aliases.c.id == saved.id)
                .values(revision=2, food_version_id=unsealed, updated_action_key="target-change")
            )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            await connection.execute(
                sa.update(food_aliases)
                .where(food_aliases.c.id == saved.id)
                .values(revision=2, updated_action_key="missing-action")
            )


def test_alias_migration_preserves_populated_ledger_drafts_and_all_existing_tables(settings):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.attributes["database_url"] = settings.resolved_database_url
    # Seed with the current writer, then obtain the genuine historical schema.
    command.upgrade(config, "head")

    async def populate():
        store = Store(settings)
        try:
            async with store.write() as connection:
                food = await publish_reviewed_food(connection, reviewed_food())
                await create(connection, food)
                await action(connection, "draft-create")
                await connection.execute(
                    sa.insert(meal_drafts).values(
                        revision=1,
                        state="open",
                        content={"synthetic": "pending"},
                        source_chat_id=101,
                        source_message_id=99,
                        created_action_key="draft-create",
                        created_at=WHEN,
                        last_user_activity_at=WHEN,
                    )
                )
        finally:
            await store.close()

    asyncio.run(populate())
    command.downgrade(config, "0006_meal_drafts")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        before = {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
    command.upgrade(config, "0007_food_aliases")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert {
            name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables
        } == before
        assert connection.execute("SELECT * FROM food_aliases").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, "0006_meal_drafts")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert {
            name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables
        } == before
