"""Fixed portions survive source changes; reuse never transfers prior approval."""

import asyncio
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database.favorites import (
    create_favorite,
    find_favorite,
    get_favorite,
    list_favorites,
    revise_favorite,
)
from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
from nutrition_bot.adapters.database.meals import revise_meal
from nutrition_bot.adapters.database.schema import food_versions, meals
from nutrition_bot.adapters.database.schema_favorites import (
    favorite_items,
    favorite_versions,
    favorites,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.domain.drafts import PlannedItem
from nutrition_bot.domain.reuse import (
    ReuseError,
    normalize_favorite_name,
    planned_from_meal,
    scale_items,
)
from tests.test_estimate_ledger import approved
from tests.test_food_storage import reviewed_food
from tests.test_meal_ledger import MIGRATIONS, action, create, measured


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


async def save_favorite(connection, meal, *, key="favorite", name="Usual breakfast"):
    await action(connection, key)
    return await create_favorite(connection, name, meal, action_key=key)


def portion(mass=125000, *, basis=None):
    return PlannedItem(
        food_version_id=1,
        edible_milligrams=mass,
        original_quantity=format(Decimal(mass) / 1000, "f"),
        original_unit="g",
        estimate_basis=basis,
    )


async def test_creation_captures_fixed_mass_source_version_and_restart(store, settings, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal, name="  Usual   breakfast ")
    reopened = Store(settings)
    try:
        async with reopened.engine.connect() as connection:
            assert await get_favorite(connection, saved.id) == saved
            assert await find_favorite(connection, "usual  BREAKFAST") == saved
    finally:
        await reopened.close()
    assert saved.name == "Usual breakfast"
    assert saved.version_number == 1
    assert saved.items[0].food_version_id == food.version_id
    assert saved.items[0].edible_milligrams == 125125
    assert saved.items[0].original_quantity == "125.125"
    assert saved.items[0].original_unit == "g"
    assert saved.items[0].estimate_basis is None


async def test_catalog_refresh_cannot_change_favorite_or_unknown_zero_values(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal)
        newer = await publish_reviewed_food(
            connection,
            reviewed_food(name="Synthetic refreshed food", basis_grams="100"),
            food_id=food.food_id,
        )
        assert newer.version_id != food.version_id
        assert await get_favorite(connection, saved.id) == saved
        pinned = await get_food_version(connection, saved.items[0].food_version_id)
        assert pinned.amount_for("energy", 125125) == Decimal("250.25")
        assert pinned.amount_for("sodium", 125125) == Decimal(0)
        assert pinned.amount_for("vitamin_d", 125125) is None
        assert pinned.amount_for("potassium", 125125) is None


async def test_update_archive_restore_create_new_versions_and_preserve_all_history(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        first = await save_favorite(connection, meal)
        before = (await connection.execute(sa.select(favorite_items))).mappings().all()
        await action(connection, "meal-change")
        meal = await revise_meal(
            connection,
            meal.id,
            meal.revision_id,
            action_key="meal-change",
            items=(measured(food, "200"),),
        )
        await action(connection, "update")
        updated = await revise_favorite(
            connection, first.id, first.version_id, action_key="update", meal=meal
        )
        assert updated.version_number == 2
        assert updated.items[0].edible_milligrams == 200000
        assert updated.version_id != first.version_id
        historic = (
            (
                await connection.execute(
                    sa.select(favorite_items).where(favorite_items.c.version_id == first.version_id)
                )
            )
            .mappings()
            .all()
        )
        assert historic == before
        await action(connection, "archive")
        archived = await revise_favorite(
            connection, first.id, updated.version_id, action_key="archive", archived=True
        )
        assert archived.version_number == 3 and archived.archived
        assert archived.items == updated.items
        assert await find_favorite(connection, first.name) is None
        assert await list_favorites(connection) == ()
        assert await list_favorites(connection, include_archived=True) == (archived,)
        await action(connection, "restore")
        restored = await revise_favorite(
            connection, first.id, archived.version_id, action_key="restore", archived=False
        )
        assert restored.version_number == 4 and not restored.archived
        assert await find_favorite(connection, first.name) == restored
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(favorite_versions)) == 4
        )


async def test_stale_version_and_replayed_actions_cannot_overwrite(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal)
        await action(connection, "archive")
        latest = await revise_favorite(
            connection, saved.id, saved.version_id, action_key="archive", archived=True
        )
        await action(connection, "stale")
        with pytest.raises(ReuseError, match="changed"):
            await revise_favorite(
                connection, saved.id, saved.version_id, action_key="stale", archived=False
            )
        with pytest.raises(ReuseError, match="already applied"):
            await revise_favorite(
                connection, saved.id, latest.version_id, action_key="archive", archived=False
            )
        assert await get_favorite(connection, saved.id) == latest


async def test_duplicate_names_include_archived_and_unicode_equivalents(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal, name="Café breakfast")
        await action(connection, "archive")
        await revise_favorite(
            connection, saved.id, saved.version_id, action_key="archive", archived=True
        )
        await action(connection, "duplicate")
        with pytest.raises(ReuseError, match="already exists"):
            await create_favorite(
                connection, " CAFE\u0301  BREAKFAST ", meal, action_key="duplicate"
            )


async def test_reject_deleted_or_stale_meal_and_reload_authoritative_content(store, food):
    async with store.write() as connection:
        original = await create(connection, food)
        forged = original.model_copy(update={"items": (), "label": "Forged"})
        first = await save_favorite(connection, forged)
        assert first.items == planned_from_meal(original)
        await action(connection, "meal-change")
        changed = await revise_meal(
            connection,
            original.id,
            original.revision_id,
            action_key="meal-change",
            items=(measured(food, "200"),),
        )
        await action(connection, "stale")
        with pytest.raises(ReuseError, match="meal changed"):
            await create_favorite(connection, "Other breakfast", original, action_key="stale")
        with pytest.raises(ReuseError, match="meal changed"):
            await revise_favorite(
                connection, first.id, first.version_id, action_key="stale", meal=original
            )
        await action(connection, "meal-delete")
        deleted = await revise_meal(
            connection,
            changed.id,
            changed.revision_id,
            action_key="meal-delete",
            deleted=True,
            operation="delete",
        )
        await action(connection, "deleted")
        with pytest.raises(ReuseError, match="deleted meal"):
            await create_favorite(connection, "Other breakfast", deleted, action_key="deleted")


async def test_estimated_portion_keeps_basis_but_never_prior_approval_authority(store, food):
    async with store.write() as connection:
        meal = await approved(connection, food)
        saved = await save_favorite(connection, meal)
        assert saved.items[0].estimate_basis == meal.items[0].quantity_basis
        assert saved.items[0].edible_milligrams == meal.items[0].edible_milligrams
        assert "approval" not in str(saved.model_dump())
        assert "callback:approved" not in str(saved.model_dump())
        scaled = scale_items(saved.items, Decimal("2"))
        assert scaled[0].edible_milligrams == 250250
        assert scaled[0].estimate_basis == saved.items[0].estimate_basis
        assert "approval" not in str(scaled[0].model_dump())


async def test_list_has_stable_name_order_and_limit(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        z = await save_favorite(connection, meal, key="z", name="Zulu")
        a = await save_favorite(connection, meal, key="a", name="Alpha")
        assert await list_favorites(connection) == (a, z)
        assert await list_favorites(connection, limit=1) == (a,)
        assert await get_favorite(connection, 999) is None
        assert await find_favorite(connection, "missing") is None


@pytest.mark.parametrize("limit", [0, 51, True, 1.5])
async def test_list_rejects_invalid_limit(store, limit):
    async with store.engine.connect() as connection:
        with pytest.raises(ReuseError):
            await list_favorites(connection, limit=limit)


@pytest.mark.parametrize("favorite_id", [0, -1, True, 2**63])
async def test_get_rejects_invalid_identity(store, favorite_id):
    async with store.engine.connect() as connection:
        with pytest.raises(ReuseError):
            await get_favorite(connection, favorite_id)


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "a" * 61,
        "a\nb",
        "a\tb",
        "\u200bname",
        "a=b",
        "F1",
        "f1v2",
        "meal x2",
        "meal today",
        "meal yesterday",
        "meal 2024-01-01",
    ],
)
def test_favorite_name_rejects_ambiguous_or_nonprintable_syntax(name):
    with pytest.raises(ReuseError):
        normalize_favorite_name(name)


def test_name_normalization_preserves_unicode_identity_and_collapses_spaces():
    assert normalize_favorite_name("  CAFE\u0301  BREAKFAST ") == "café breakfast"
    assert normalize_favorite_name("Սովորական նախաճաշ") == "սովորական նախաճաշ"
    assert normalize_favorite_name("Straße") == "strasse"


@pytest.mark.parametrize(
    "factor",
    [
        Decimal(0),
        Decimal(-1),
        Decimal("100.001"),
        Decimal("0.0001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        1.1,
        1,
        "1",
        True,
    ],
)
def test_scaling_rejects_unsafe_factor(factor):
    with pytest.raises(ReuseError):
        scale_items((portion(),), factor)


@pytest.mark.parametrize(("mass", "factor"), [(1, "0.5"), (333, "1.1"), (50_000_000, "1.001")])
def test_scaling_rejects_fractional_milligrams_and_mass_overflow(mass, factor):
    with pytest.raises(ReuseError, match="exact milligram"):
        scale_items((portion(mass),), Decimal(factor))


def test_scaling_is_exact_and_does_not_mutate_prior_portions():
    original = (portion(250000), portion(125000, basis="Synthetic estimate"))
    scaled = scale_items(original, Decimal("1.125"))
    assert [item.edible_milligrams for item in scaled] == [281250, 140625]
    assert [item.original_quantity for item in scaled] == ["281.25", "140.625"]
    assert scaled[1].estimate_basis == original[1].estimate_basis
    assert [item.edible_milligrams for item in original] == [250000, 125000]


def test_scaling_cannot_fill_unresolved_portions():
    with pytest.raises(ReuseError, match="Resolve"):
        scale_items((PlannedItem(food_version_id=1),), Decimal(1))


@pytest.mark.parametrize(
    "mutation",
    [
        "identity",
        "version",
        "item-update",
        "item-insert",
        "item-delete",
        "version-delete",
        "current-pointer",
        "favorite-replace",
        "version-replace",
        "item-replace",
    ],
)
async def test_database_blocks_history_mutation_and_replace(store, food, mutation):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal)
        statements = {
            "identity": sa.update(favorites).values(name="Changed"),
            "version": sa.update(favorite_versions).values(archived=True),
            "item-update": sa.update(favorite_items).values(edible_milligrams=1),
            "item-insert": sa.insert(favorite_items).values(
                version_id=saved.version_id,
                item_index=1,
                food_version_id=food.version_id,
                edible_milligrams=1000,
            ),
            "item-delete": sa.delete(favorite_items),
            "version-delete": sa.delete(favorite_versions),
            "current-pointer": sa.update(favorites).values(current_version_id=None),
        }
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                if mutation.endswith("replace"):
                    table = {
                        "favorite-replace": "favorites",
                        "version-replace": "favorite_versions",
                        "item-replace": "favorite_items",
                    }[mutation]
                    await connection.exec_driver_sql(
                        f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                    )
                else:
                    await connection.execute(statements[mutation])
        assert await get_favorite(connection, saved.id) == saved


async def test_food_pin_survives_deleted_source_meal_and_releases_on_whole_favorite_erasure(
    store, food
):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal)
        await connection.execute(sa.delete(meals).where(meals.c.id == meal.id))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    sa.delete(food_versions).where(food_versions.c.id == food.version_id)
                )
        assert await get_favorite(connection, saved.id) == saved
        await connection.execute(sa.delete(favorites).where(favorites.c.id == saved.id))
        assert await get_favorite(connection, saved.id) is None
        assert await connection.scalar(sa.select(sa.func.count()).select_from(favorite_items)) == 0
        await connection.execute(
            sa.delete(food_versions).where(food_versions.c.id == food.version_id)
        )
        assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []


async def test_new_version_cannot_seal_without_complete_items_or_move_backwards(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal)
        await action(connection, "incomplete")
        async with connection.begin_nested() as savepoint:
            version_id = (
                await connection.execute(
                    sa.insert(favorite_versions)
                    .values(
                        favorite_id=saved.id,
                        version_number=2,
                        previous_version_id=saved.version_id,
                        action_key="incomplete",
                        archived=False,
                        sealed=False,
                        created_at=0,
                    )
                    .returning(favorite_versions.c.id)
                )
            ).scalar_one()
            with pytest.raises(IntegrityError, match="incomplete"):
                await connection.execute(
                    sa.update(favorite_versions)
                    .where(favorite_versions.c.id == version_id)
                    .values(sealed=True)
                )
            await connection.execute(
                sa.insert(favorite_items).values(
                    version_id=version_id,
                    item_index=2,
                    food_version_id=food.version_id,
                    edible_milligrams=1000,
                )
            )
            with pytest.raises(IntegrityError, match="incomplete"):
                await connection.execute(
                    sa.update(favorite_versions)
                    .where(favorite_versions.c.id == version_id)
                    .values(sealed=True)
                )
            with pytest.raises(IntegrityError):
                await connection.execute(
                    sa.update(favorites)
                    .where(favorites.c.id == saved.id)
                    .values(current_version_id=version_id)
                )
            await savepoint.rollback()
        await action(connection, "archive")
        latest = await revise_favorite(
            connection, saved.id, saved.version_id, action_key="archive", archived=True
        )
        with pytest.raises(IntegrityError):
            await connection.execute(
                sa.update(favorites)
                .where(favorites.c.id == saved.id)
                .values(current_version_id=saved.version_id)
            )
        assert await get_favorite(connection, saved.id) == latest


async def test_populated_upgrade_and_downgrade_preserve_existing_ledger(settings):
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    await asyncio.to_thread(command.upgrade, config, "head")
    store = Store(settings)
    try:
        async with store.write() as connection:
            food = await publish_reviewed_food(connection, reviewed_food())
            meal = await create(connection, food)
    finally:
        await store.close()
    await asyncio.to_thread(command.downgrade, config, "0007_food_aliases")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        tables = (
            "foods",
            "food_versions",
            "meals",
            "meal_revisions",
            "meal_items",
            "meal_item_nutrients",
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        }
    await asyncio.to_thread(command.upgrade, config, "head")
    store = Store(settings)
    try:
        async with store.write() as connection:
            saved = await save_favorite(connection, meal)
            await action(connection, "archive")
            await revise_favorite(
                connection, saved.id, saved.version_id, action_key="archive", archived=True
            )
    finally:
        await store.close()
    await asyncio.to_thread(command.downgrade, config, "0007_food_aliases")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert {
            table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables
        } == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'favorite%'"
        ).fetchall()


async def test_explicit_rowid_replace_cannot_steal_historic_portion(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        saved = await save_favorite(connection, meal)
        await action(connection, "draft")
        async with connection.begin_nested() as savepoint:
            version_id = (
                await connection.execute(
                    sa.insert(favorite_versions)
                    .values(
                        favorite_id=saved.id,
                        version_number=2,
                        previous_version_id=saved.version_id,
                        action_key="draft",
                        archived=False,
                        sealed=False,
                        created_at=0,
                    )
                    .returning(favorite_versions.c.id)
                )
            ).scalar_one()
            with pytest.raises(IntegrityError, match="immutable favorite identity"):
                await connection.exec_driver_sql(
                    "INSERT OR REPLACE INTO favorite_items "
                    "(rowid,version_id,item_index,food_version_id,"
                    "edible_milligrams,estimate_basis) "
                    "SELECT rowid,?,item_index,food_version_id,edible_milligrams,estimate_basis "
                    "FROM favorite_items LIMIT 1",
                    (version_id,),
                )
            assert await get_favorite(connection, saved.id) == saved
            await savepoint.rollback()


@pytest.mark.parametrize("name", ["a\u2028b", "a\u2029b"])
def test_favorite_names_reject_unicode_line_and_paragraph_separators(name):
    with pytest.raises(ReuseError):
        normalize_favorite_name(name)


async def test_missing_estimate_provenance_cannot_be_silently_marked_measured(store, food):
    async with store.write() as connection:
        meal = await approved(connection, food)
    invalid = meal.model_copy(
        update={"items": (meal.items[0].model_copy(update={"quantity_basis": None}),)}
    )
    with pytest.raises(ReuseError, match="provenance"):
        planned_from_meal(invalid)


def test_scaling_smallest_and_largest_supported_boundaries():
    assert scale_items((portion(1000),), Decimal("0.001"))[0].edible_milligrams == 1
    assert scale_items((portion(500000),), Decimal(100))[0].edible_milligrams == 50_000_000


@pytest.mark.parametrize("name", ["today breakfast", "YESTERDAY breakfast", "2024-01-15 breakfast"])
def test_favorite_names_reject_leading_reuse_date_syntax(name):
    with pytest.raises(ReuseError, match="dates"):
        normalize_favorite_name(name)
