"""An estimate stays visibly estimated, and approvals cannot authorize another portion."""

import asyncio
import math
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.meals import (
    MealError,
    MealItemInput,
    get_meal,
    input_from_snapshot,
    revise_meal,
    undo_meal,
)
from nutrition_bot.adapters.database.schema import meal_items
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.cli import migrate
from tests.test_food_storage import reviewed_food
from tests.test_meal_ledger import DAY, MIGRATIONS, WHEN, action, create, measured


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


def estimated(food, grams="125.125", **changes):
    return replace(
        measured(food, grams),
        quantity_method="approved_estimate",
        quantity_basis="Synthetic portion fixture; edible mass is an estimate.",
        approval_action_key="callback:approved",
        approved_at=WHEN,
        approval_draft_id=7,
        approval_draft_revision=2,
        **changes,
    )


async def approved(connection, food, **kwargs):
    values = dict(items=(estimated(food),), action_key="callback:approved")
    values.update(kwargs)
    return await create(connection, food, **values)


async def test_approval_provenance_and_scaled_values_survive_read(store, food):
    async with store.write() as connection:
        first = await approved(connection, food)
    async with store.engine.connect() as connection:
        fetched = await get_meal(connection, first.id)
    assert fetched == first
    assert input_from_snapshot(fetched.items[0]) == estimated(food)
    assert fetched.items[0].quantity_method == "approved_estimate"
    nutrients = {n.code: n.amount_scaled for n in fetched.items[0].nutrients}
    assert nutrients["energy"] == 250250000
    assert nutrients["vitamin_d"] is None
    assert nutrients["sodium"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity_method", "inferred"),
        ("quantity_basis", None),
        ("quantity_basis", ""),
        ("quantity_basis", " \t\n"),
        ("quantity_basis", "a" * 301),
        ("approval_action_key", None),
        ("approval_action_key", "update:1"),
        ("approval_action_key", "callback:"),
        ("approved_at", None),
        ("approved_at", True),
        ("approved_at", 0),
        ("approved_at", -1),
        ("approved_at", math.nan),
        ("approved_at", math.inf),
        ("approved_at", 10**1000),
        ("approval_draft_id", None),
        ("approval_draft_id", True),
        ("approval_draft_id", 0),
        ("approval_draft_id", 2**63),
        ("approval_draft_revision", None),
        ("approval_draft_revision", True),
        ("approval_draft_revision", 0),
        ("approval_draft_revision", 2**63),
    ],
)
async def test_incomplete_or_invalid_approval_cannot_save(store, food, field, value):
    async with store.write() as connection:
        with pytest.raises(MealError):
            await approved(connection, food, items=(replace(estimated(food), **{field: value}),))


@pytest.mark.parametrize(
    "field",
    [
        "quantity_basis",
        "approval_action_key",
        "approved_at",
        "approval_draft_id",
        "approval_draft_revision",
    ],
)
async def test_measured_items_cannot_silently_keep_estimate_metadata(store, food, field):
    input_value = replace(measured(food), **{field: getattr(estimated(food), field)})
    async with store.write() as connection:
        with pytest.raises(MealError, match="measured amount"):
            await create(connection, food, items=(input_value,))


async def test_previous_approval_cannot_authorize_a_new_meal(store, food):
    async with store.write() as connection:
        await approved(connection, food)
        with pytest.raises(MealError, match="fresh approval"):
            await create(
                connection,
                food,
                items=(estimated(food),),
                action_key="another-meal",
                source_message_id=12,
            )


@pytest.mark.parametrize(
    "change",
    [
        {"edible_milligrams": 100000, "original_quantity": "100"},
        {"quantity_basis": "A different estimate basis"},
        {"approval_draft_revision": 3},
        {"approved_at": WHEN + 10},
        {"original_unit": "G"},
    ],
)
async def test_changed_estimates_cannot_reuse_old_approval(store, food, change):
    async with store.write() as connection:
        first = await approved(connection, food)
        await action(connection, "correction")
        with pytest.raises(MealError, match="fresh approval"):
            await revise_meal(
                connection,
                first.id,
                first.revision_id,
                action_key="correction",
                items=(replace(input_from_snapshot(first.items[0]), **change),),
            )
        assert await get_meal(connection, first.id) == first


async def test_changed_food_requires_fresh_approval(store, food):
    async with store.write() as connection:
        first = await approved(connection, food)
        other = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic different food")
        )
        await action(connection, "correction")
        with pytest.raises(MealError, match="fresh approval"):
            await revise_meal(
                connection,
                first.id,
                first.revision_id,
                action_key="correction",
                items=(replace(estimated(food), food_version_id=other.version_id),),
            )


async def test_duplicate_estimated_item_requires_fresh_approval(store, food):
    async with store.write() as connection:
        first = await approved(connection, food)
        await action(connection, "duplicate")
        with pytest.raises(MealError, match="fresh approval"):
            await revise_meal(
                connection,
                first.id,
                first.revision_id,
                action_key="duplicate",
                items=(estimated(food), estimated(food)),
            )


async def test_unchanged_estimate_survives_unrelated_item_edit_and_reordering(store, food):
    async with store.write() as connection:
        first = await approved(connection, food, items=(estimated(food), measured(food, "50")))
        await action(connection, "correction")
        changed = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="correction",
            items=(measured(food, "75"), input_from_snapshot(first.items[0])),
        )
    assert changed.items[1] == first.items[0]
    assert changed.items[0].quantity_method == "measured"
    assert changed.items[0].edible_milligrams == 75000


async def test_changed_estimate_accepts_current_callback_provenance(store, food):
    async with store.write() as connection:
        first = await approved(connection, food)
        await action(connection, "callback:correction")
        item = replace(
            estimated(food, "200"),
            approval_action_key="callback:correction",
            approval_draft_id=8,
            approval_draft_revision=3,
            approved_at=WHEN + 60,
        )
        changed = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="callback:correction",
            items=(item,),
        )
    assert input_from_snapshot(changed.items[0]) == item


async def test_measured_correction_clears_estimate_but_undo_restores_it(store, food):
    async with store.write() as connection:
        first = await approved(connection, food)
        await action(connection, "measured")
        changed = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="measured",
            items=(MealItemInput(food.version_id, 80000, "80", "g"),),
        )
        await action(connection, "undo")
        restored = await undo_meal(connection, first.id, changed.revision_id, action_key="undo")
    assert changed.items[0].quantity_method == "measured"
    assert changed.items[0].approval_action_key is None
    assert changed.items[0].quantity_basis is None
    assert restored.items == first.items


async def test_date_delete_undo_preserve_approved_snapshot(store, food):
    async with store.write() as connection:
        first = await approved(connection, food)
        await action(connection, "date")
        changed = await revise_meal(
            connection, first.id, first.revision_id, action_key="date", local_date=date(2024, 1, 14)
        )
        await action(connection, "delete")
        deleted = await revise_meal(
            connection, first.id, changed.revision_id, action_key="delete", operation="delete"
        )
        await action(connection, "undo")
        restored = await undo_meal(connection, first.id, deleted.revision_id, action_key="undo")
    assert first.items == changed.items == deleted.items == restored.items
    assert not restored.deleted and restored.local_date == date(2024, 1, 14)


def migration_config(settings):
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    return config


async def seeded_meal(settings, *, estimate=False):
    store = Store(settings)
    try:
        async with store.write() as connection:
            food = await publish_reviewed_food(connection, reviewed_food())
            first = await (approved(connection, food) if estimate else create(connection, food))
            await action(connection, "second")
            await revise_meal(
                connection, first.id, first.revision_id, action_key="second", label="Revised"
            )
    finally:
        await store.close()


def table_data(connection, name):
    return connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()


def test_populated_0004_upgrade_and_measured_downgrade_preserve_data_and_guards(settings):
    migrate(settings)
    asyncio.run(seeded_meal(settings))
    config = migration_config(settings)
    command.downgrade(config, "0004_meal_ledger")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        names = (
            "meals",
            "meal_revisions",
            "meal_items",
            "meal_item_nutrients",
            "actions",
            "food_versions",
        )
        before = {name: table_data(connection, name) for name in names}
        guards = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
        assert len(before["meal_items"]) == 2
    command.upgrade(config, "0005_approved_estimates")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for name in names:
            actual = table_data(connection, name)
            if name == "meal_items":
                assert [row[:-5] for row in actual] == before[name]
                assert all(row[-5:] == (None,) * 5 for row in actual)
            else:
                assert actual == before[name]
        assert (
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
            == guards
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        with pytest.raises(sqlite3.IntegrityError, match="immutable meal snapshot"):
            connection.execute("UPDATE meal_items SET quantity_method='approved_estimate'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable meal snapshot"):
            connection.execute("DELETE FROM meal_item_nutrients")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM actions")
    command.downgrade(config, "0004_meal_ledger")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert {name: table_data(connection, name) for name in names} == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_downgrade_refuses_to_discard_any_estimate_history(settings):
    migrate(settings)
    asyncio.run(seeded_meal(settings, estimate=True))
    config = migration_config(settings)
    # Newer feature migrations may be present; isolate this migration's rejection.
    command.downgrade(config, "0005_approved_estimates")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        before = table_data(connection, "meal_items")
    with pytest.raises(RuntimeError, match="estimate approval history"):
        command.downgrade(config, "0004_meal_ledger")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert table_data(connection, "meal_items") == before
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0005_approved_estimates",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity_method": "approved_estimate"},
        {"quantity_basis": "Unexpected measured metadata"},
        {"quantity_method": "inferred"},
        {
            "quantity_method": "approved_estimate",
            "quantity_basis": "Synthetic estimate",
            "approval_action_key": "callback:dbcheck",
            "approved_at": math.inf,
            "approval_draft_id": 1,
            "approval_draft_revision": 1,
        },
    ],
)
async def test_database_rejects_incomplete_estimate_metadata(store, food, changes):
    from nutrition_bot.adapters.database.schema import meal_revisions

    async with store.write() as connection:
        first = await create(connection, food)
        await action(connection, "callback:dbcheck")
        revision_id = (
            await connection.execute(
                sa.insert(meal_revisions)
                .values(
                    meal_id=first.id,
                    revision_number=2,
                    previous_revision_id=first.revision_id,
                    action_key="callback:dbcheck",
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
        values = first.items[0].model_dump(mode="json", exclude={"nutrients"})
        values.update(changes)
        with pytest.raises(IntegrityError, match="meal_quantity_method"):
            await connection.execute(
                sa.insert(meal_items).values(revision_id=revision_id, item_index=0, **values)
            )
        await connection.rollback()


def test_failed_rebuild_rolls_back_to_intact_populated_0004(settings, monkeypatch):
    migrate(settings)
    asyncio.run(seeded_meal(settings))
    config = migration_config(settings)
    command.downgrade(config, "0004_meal_ledger")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        before_items = table_data(connection, "meal_items")
        before_nutrients = table_data(connection, "meal_item_nutrients")
        before_guards = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
    execute = sa.engine.Connection.exec_driver_sql

    def fail_guard_recreation(connection, statement, *args, **kwargs):
        if statement.startswith("CREATE TRIGGER meal_items_insert_sealed"):
            raise RuntimeError("Synthetic interrupted table rebuild")
        return execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Connection, "exec_driver_sql", fail_guard_recreation)
    with pytest.raises(RuntimeError, match="Synthetic interrupted"):
        command.upgrade(config, "0005_approved_estimates")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert table_data(connection, "meal_items") == before_items
        assert table_data(connection, "meal_item_nutrients") == before_nutrients
        assert (
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ).fetchall()
            == before_guards
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0004_meal_ledger",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
