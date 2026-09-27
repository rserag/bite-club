import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database import meals as ledger
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.meals import (
    MealError,
    MealItemInput,
    create_meal,
    get_meal,
    revise_meal,
    undo_meal,
)
from nutrition_bot.adapters.database.schema import (
    SCHEMA_REVISION,
    actions,
    food_versions,
    inbox,
    meal_item_nutrients,
    meal_items,
    meal_revisions,
    meals,
)
from nutrition_bot.cli import migrate
from tests.test_food_storage import reviewed_food

MIGRATIONS = str(Path(__file__).resolve().parents[1] / "migrations")
DAY = date(2024, 1, 15)
WHEN = datetime(2024, 1, 15, 12, tzinfo=ZoneInfo("UTC")).timestamp()


async def action(connection, key):
    update_id = 1 + (await connection.scalar(sa.select(sa.func.count()).select_from(inbox)))
    await connection.execute(
        sa.insert(inbox).values(
            update_id=update_id,
            payload=None,
            status="done",
            received_at=WHEN,
            processed_at=WHEN,
        )
    )
    await connection.execute(
        sa.insert(actions).values(
            key=key,
            update_id=update_id,
            kind="synthetic_meal",
            created_at=WHEN,
        )
    )


def measured(food, grams="125.125"):
    return MealItemInput(food.version_id, int(Decimal(grams) * 1000), grams, "g")


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


async def create(connection, food, **kwargs):
    values = dict(
        items=(measured(food),),
        label="Synthetic lunch",
        local_date=DAY,
        timezone="UTC",
        consumed_at=WHEN,
        action_key="create",
        source_chat_id=101,
        source_message_id=10,
    )
    values.update(kwargs)
    await action(connection, values["action_key"])
    return await create_meal(connection, **values)


async def test_create_exact_consumed_snapshot_keeps_zero_missing_and_units(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
    async with store.engine.connect() as connection:
        assert await get_meal(connection, meal.id) == meal
    item = meal.items[0]
    assert item.food_name == food.record.name
    assert item.preparation == "raw"
    assert item.source_reference == food.record.source_reference
    assert item.food_content_sha256 == food.content_sha256
    assert item.edible_milligrams == 125125
    assert item.original_quantity == "125.125"
    assert item.quantity_method == "measured"
    nutrients = {value.code: value for value in item.nutrients}
    assert nutrients["energy"].amount_scaled == 250250000
    assert nutrients["protein"].amount_scaled == 12512500
    assert nutrients["protein"].unit == "g"
    assert nutrients["sodium"].amount_scaled == 0
    assert nutrients["vitamin_d"].amount_scaled is None
    assert "potassium" not in nutrients
    assert all(value.quality == "manual_reviewed" for value in item.nutrients)
    assert meal.revision_number == 1 and meal.operation == "create" and not meal.deleted


@pytest.mark.parametrize(("nutrient", "expected"), [("0.000001", 0), ("0.000003", 2)])
async def test_consumed_snapshot_rounds_ties_to_even_once(store, nutrient, expected):
    async with store.write() as connection:
        record = await publish_reviewed_food(
            connection,
            reviewed_food(
                basis_grams="100", nutrients=[{"code": "protein", "unit": "g", "amount": nutrient}]
            ),
        )
        meal = await create(connection, record, items=(measured(record, "50"),))
    assert meal.items[0].nutrients[0].amount_scaled == expected


async def test_quantity_correction_keeps_original_revision_and_selected_food_version(store, food):
    async with store.write() as connection:
        first = await create(connection, food)
        updated_food = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic changed catalog"), food_id=food.food_id
        )
        await action(connection, "edit")
        second = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="edit",
            items=(measured(food, "50"),),
        )
        historic = await ledger._get_revision(connection, first.revision_id)
        current = await get_meal(connection, first.id)
    assert current == second
    assert historic == first
    assert second.items[0].food_version_id == food.version_id != updated_food.version_id
    assert second.items[0].food_name == food.record.name
    assert second.items[0].edible_milligrams == 50000
    assert second.revision_number == 2


async def test_date_delete_undo_copy_snapshots_without_food_lookups(store, food, monkeypatch):
    async with store.write() as connection:
        first = await create(connection, food)

        async def forbidden(*args):
            raise AssertionError("Historical changes must not read catalog values")

        monkeypatch.setattr(ledger, "get_food_version", forbidden)
        await action(connection, "date")
        changed = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="date",
            local_date=date(2024, 1, 14),
            label="Synthetic breakfast",
        )
        assert changed.items == first.items
        assert changed.local_date == date(2024, 1, 14)
        assert changed.consumed_at == WHEN - 86400
        await action(connection, "delete")
        deleted = await revise_meal(
            connection, first.id, changed.revision_id, action_key="delete", operation="delete"
        )
        assert deleted.deleted and deleted.items == first.items
        await action(connection, "undo")
        restored = await undo_meal(connection, first.id, deleted.revision_id, action_key="undo")
        assert not restored.deleted
        assert restored.items == changed.items
        assert restored.label == changed.label
        assert restored.local_date == changed.local_date
        assert restored.consumed_at == changed.consumed_at
        assert restored.revision_number == 4


async def test_undo_quantity_restores_exact_predecessor(store, food):
    async with store.write() as connection:
        first = await create(connection, food)
        await action(connection, "edit")
        changed = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="edit",
            items=(measured(food, "17.123"),),
        )
        await action(connection, "undo")
        restored = await undo_meal(connection, first.id, changed.revision_id, action_key="undo")
    assert restored.items == first.items
    assert restored.operation == "undo"


async def test_undo_creation_deletes_and_repeated_undo_is_rejected(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        await action(connection, "undo")
        deleted = await undo_meal(connection, meal.id, meal.revision_id, action_key="undo")
        assert deleted.deleted and deleted.items == meal.items
        await action(connection, "again")
        with pytest.raises(MealError, match="already undone"):
            await undo_meal(connection, meal.id, deleted.revision_id, action_key="again")
        assert await get_meal(connection, meal.id) == deleted


async def test_stale_revision_duplicate_action_and_source_message_are_rejected(store, food):
    async with store.write() as connection:
        meal = await create(connection, food)
        await action(connection, "edit")
        edited = await revise_meal(
            connection, meal.id, meal.revision_id, action_key="edit", label="Synthetic edited"
        )
        await action(connection, "stale")
        with pytest.raises(MealError, match="changed since"):
            await revise_meal(connection, meal.id, meal.revision_id, action_key="stale")
        with pytest.raises(MealError, match="already applied"):
            await revise_meal(connection, meal.id, edited.revision_id, action_key="edit")
        with pytest.raises(MealError, match="already has a meal"):
            await create(connection, food, action_key="duplicate")
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 1
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_revisions)) == 2


async def test_missing_action_and_missing_meal_are_safe_errors(store, food):
    async with store.write() as connection:
        with pytest.raises(MealError, match="unavailable"):
            await get_meal(connection, 50)
        with pytest.raises(MealError, match="action is unavailable"):
            await create_meal(
                connection,
                items=(measured(food),),
                label="Synthetic",
                local_date=DAY,
                timezone="UTC",
                consumed_at=WHEN,
                action_key="missing",
                source_chat_id=101,
                source_message_id=10,
            )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


@pytest.mark.parametrize(
    ("mass", "quantity", "unit"),
    [
        (0, "0", "g"),
        (-1, "1", "g"),
        (True, "1", "mg"),
        (1.5, "1.5", "mg"),
        (50_000_001, "50000.001", "g"),
        (1000, "2", "g"),
        (1000, "NaN", "g"),
        (1000, "1", "cups"),
        (1000, "-1", "g"),
        (1000, "0.0011", "kg"),
    ],
)
async def test_reject_invalid_measured_mass_without_partial_meal(store, food, mass, quantity, unit):
    async with store.write() as connection:
        with pytest.raises(MealError):
            await create(
                connection, food, items=(MealItemInput(food.version_id, mass, quantity, unit),)
            )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


@pytest.mark.parametrize("count", [0, 11])
async def test_reject_empty_or_overlong_meal(store, food, count):
    async with store.write() as connection:
        with pytest.raises(MealError, match="between 1 and 10"):
            await create(connection, food, items=(measured(food),) * count)
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


async def test_reject_unspecified_preparation_and_unavailable_food(store):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, reviewed_food(preparation="unspecified"))
        with pytest.raises(MealError, match="clear raw, cooked"):
            await create(connection, food)
        with pytest.raises(MealError, match="food version is unavailable"):
            await create(
                connection,
                food,
                action_key="missing-food",
                items=(MealItemInput(999, 1000, "1", "g"),),
            )


async def test_reject_consumed_nutrient_overflow_before_any_meal_write(store):
    async with store.write() as connection:
        food = await publish_reviewed_food(
            connection,
            reviewed_food(
                basis_grams="0.1",
                nutrients=[{"code": "energy", "unit": "kcal", "amount": "1000000000"}],
            ),
        )
        with pytest.raises(MealError, match="nutrient range"):
            await create(connection, food, items=(measured(food, "50000"),))
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"label": " "},
        {"label": "a" * 121},
        {"timezone": "Missing/Synthetic"},
        {"consumed_at": float("nan")},
        {"consumed_at": True},
        {"local_date": date(2024, 1, 16)},
        {"source_message_id": 0},
    ],
)
async def test_reject_invalid_metadata(store, food, overrides):
    async with store.write() as connection:
        with pytest.raises(MealError):
            await create(connection, food, **overrides)


async def test_transaction_rollback_removes_meal_revisions_and_action(store, food):
    with pytest.raises(RuntimeError, match="synthetic failure"):
        async with store.write() as connection:
            await create(connection, food)
            raise RuntimeError("synthetic failure")
    async with store.engine.connect() as connection:
        for table in (meals, meal_revisions, meal_items, meal_item_nutrients, actions, inbox):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE meals SET source_message_id = 11",
        "UPDATE meal_revisions SET label = 'changed'",
        "UPDATE meal_revisions SET sealed = 0",
        "UPDATE meal_items SET edible_milligrams = 1",
        "UPDATE meal_item_nutrients SET amount_scaled = 999",
        "DELETE FROM meal_revisions",
        "DELETE FROM meal_items",
        "DELETE FROM meal_item_nutrients",
        "INSERT OR REPLACE INTO meals SELECT * FROM meals",
        "INSERT OR REPLACE INTO meal_revisions SELECT * FROM meal_revisions",
        "INSERT OR REPLACE INTO meal_items SELECT * FROM meal_items",
        "INSERT OR REPLACE INTO meal_item_nutrients SELECT * FROM meal_item_nutrients",
    ],
)
async def test_database_rejects_mutation_and_replace_of_history(store, food, statement):
    async with store.write() as connection:
        meal = await create(connection, food)
        with pytest.raises(IntegrityError):
            await connection.exec_driver_sql(statement)
        assert await get_meal(connection, meal.id) == meal


async def test_old_current_pointer_cannot_rewind_or_cross_meals(store, food):
    async with store.write() as connection:
        first = await create(connection, food)
        await action(connection, "edit")
        second = await revise_meal(
            connection, first.id, first.revision_id, action_key="edit", label="Synthetic revision"
        )
        other = await create(connection, food, action_key="other", source_message_id=11)
        for revision_id in (first.revision_id, other.revision_id, None):
            with pytest.raises(IntegrityError, match="invalid meal revision"):
                await connection.execute(
                    sa.update(meals)
                    .where(meals.c.id == first.id)
                    .values(current_revision_id=revision_id)
                )
        assert await get_meal(connection, first.id) == second


async def test_food_versions_restricted_but_complete_meal_history_can_be_erased(store, food):
    async with store.write() as connection:
        first = await create(connection, food)
        await action(connection, "edit")
        latest = await revise_meal(
            connection, first.id, first.revision_id, action_key="edit", label="Synthetic revision"
        )
        with pytest.raises(IntegrityError):
            await connection.execute(
                sa.delete(food_versions).where(food_versions.c.id == food.version_id)
            )
        await connection.execute(sa.delete(meals).where(meals.c.id == first.id))
        for table in (meal_revisions, meal_items, meal_item_nutrients):
            assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        new = await create(connection, food, action_key="new", source_message_id=12)
        assert new.id > first.id
        assert new.revision_id > latest.revision_id
        assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []


def test_upgrade_populated_0003_preserves_catalog_and_action_data(settings):
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    command.upgrade(config, "0003_food_sources")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("INSERT INTO profile VALUES (1, 'UTC', 1700000000)")
        connection.execute("INSERT INTO foods (preparation, created_at) VALUES ('raw', 1700000000)")
        connection.execute(
            "INSERT INTO food_source_cache VALUES "
            "('synthetic','fixture','hash','{}',1700000000,1700000010)"
        )
        before = {
            name: connection.execute(f"SELECT * FROM {name}").fetchall()
            for name in ("profile", "foods", "food_source_cache", "nutrients")
        }
        connection.commit()
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        after = {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in before}
        assert after == before
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            SCHEMA_REVISION,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, "0003_food_sources")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert {
            name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in before
        } == before


@pytest.mark.parametrize(
    ("mass", "quantity", "unit"),
    [(123000, "123", "G"), (123000, "0.123", "KG"), (123000, "123000", "MG")],
)
async def test_mass_units_are_case_insensitive_but_preserved(store, food, mass, quantity, unit):
    async with store.write() as connection:
        meal = await create(
            connection, food, items=(MealItemInput(food.version_id, mass, quantity, unit),)
        )
    assert meal.items[0].edible_milligrams == mass
    assert meal.items[0].original_quantity == quantity
    assert meal.items[0].original_unit == unit


async def test_explicit_rowid_replace_cannot_move_snapshot_to_unsealed_revision(store, food):
    async with store.write() as connection:
        first = await create(connection, food)
        await action(connection, "draft")
        draft = (
            await connection.execute(
                sa.insert(meal_revisions)
                .values(
                    meal_id=first.id,
                    revision_number=2,
                    previous_revision_id=first.revision_id,
                    action_key="draft",
                    label="Synthetic draft",
                    local_date=DAY,
                    timezone="UTC",
                    consumed_at=WHEN,
                    deleted=False,
                    operation="edit",
                    sealed=False,
                )
                .returning(meal_revisions.c.id)
            )
        ).scalar_one()
        for table in (meal_items, meal_item_nutrients):
            names = ",".join(column.name for column in table.columns)
            replacement = ",".join(
                str(draft) if column.name == "revision_id" else column.name
                for column in table.columns
            )
            with pytest.raises(IntegrityError, match="immutable meal identity"):
                await connection.exec_driver_sql(
                    f"INSERT OR REPLACE INTO {table.name} (rowid,{names}) "
                    f"SELECT rowid,{replacement} FROM {table.name} LIMIT 1"
                )
        with pytest.raises(IntegrityError, match="incomplete meal revision"):
            await connection.execute(
                sa.update(meal_revisions).where(meal_revisions.c.id == draft).values(sealed=True)
            )
        assert await get_meal(connection, first.id) == first
        # Fixture deliberately rolls the unsealed draft back via a savepoint-free parent rollback.
        await connection.rollback()


async def test_two_foods_keep_distinct_snapshots_and_unknowns(store, food):
    async with store.write() as connection:
        other = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic cooked food", preparation="cooked")
        )
        result = await create(
            connection, food, items=(measured(food, "100"), measured(other, "25"))
        )
    assert len(result.items) == 2
    assert result.items[0].preparation == "raw"
    assert result.items[1].preparation == "cooked"
    assert result.items[0].nutrients != result.items[1].nutrients
    assert [
        value.amount_scaled for value in result.items[0].nutrients if value.code == "vitamin_d"
    ] == [None]


async def test_local_day_is_checked_in_recorded_timezone(store, food):
    local = datetime(2024, 1, 16, 0, 15, tzinfo=ZoneInfo("Pacific/Auckland"))
    async with store.write() as connection:
        meal = await create(
            connection,
            food,
            local_date=local.date(),
            timezone="Pacific/Auckland",
            consumed_at=local.timestamp(),
        )
        assert meal.local_date == date(2024, 1, 16)
        assert datetime.fromtimestamp(meal.consumed_at, ZoneInfo("UTC")).date() == DAY


async def test_downgrade_can_remove_populated_meal_ledger_without_losing_catalog(
    store, food, settings
):
    async with store.write() as connection:
        first = await create(connection, food)
        await action(connection, "edit")
        await revise_meal(
            connection, first.id, first.revision_id, action_key="edit", label="Synthetic edited"
        )
    await store.close()
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url

    await asyncio.to_thread(command.downgrade, config, "0003_food_sources")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM food_versions").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
