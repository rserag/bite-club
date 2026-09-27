"""Immutable recipe definitions and exact rational consumed ingredient snapshots."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
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
from nutrition_bot.adapters.database.recipes import (
    create_recipe,
    get_recipe,
    get_recipe_by_number,
    get_recipe_version,
    list_recipes,
    revise_recipe,
    validate_recipe_item,
)
from nutrition_bot.adapters.database.schema import food_versions, meal_items, meals
from nutrition_bot.adapters.database.schema_recipes import (
    recipe_ingredients,
    recipe_versions,
    recipes,
)
from nutrition_bot.adapters.database.store import Store
from nutrition_bot.domain.drafts import PlannedItem
from nutrition_bot.domain.recipe_portions import RecipeError, ingredient_grams
from nutrition_bot.domain.recipes import RecipeDefinition, normalize_recipe_name, recipe_portions
from tests.test_food_storage import reviewed_food
from tests.test_meal_ledger import MIGRATIONS, WHEN, action, create


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


def ingredient(food, mass=100000, *, basis=None):
    return PlannedItem(
        food_version_id=food.version_id,
        edible_milligrams=mass,
        original_quantity=format(Decimal(mass) / 1000, "f"),
        original_unit="g",
        estimate_basis=basis,
    )


def definition(
    food, *, mass=100000, unit="serving", total=3000, ingredient_basis=None, yield_basis=None
):
    return RecipeDefinition(
        items=(ingredient(food, mass, basis=ingredient_basis),),
        unit=unit,
        total_units=total,
        estimate_basis=yield_basis,
    )


async def save_recipe(connection, food, *, key="recipe", name="Synthetic batch", **kwargs):
    await action(connection, key)
    return await create_recipe(connection, name, definition(food, **kwargs), action_key=key)


def meal_input(item, *, approval_key=None):
    assert item.edible_milligrams is not None and item.original_quantity is not None
    assert item.original_unit is not None
    values = dict(
        food_version_id=item.food_version_id,
        edible_milligrams=item.edible_milligrams,
        original_quantity=item.original_quantity,
        original_unit=item.original_unit,
        recipe_share=item.recipe_share,
    )
    if item.estimate_basis:
        values.update(
            quantity_method="approved_estimate",
            quantity_basis=item.estimate_basis,
            approval_action_key=approval_key,
            approved_at=WHEN,
            approval_draft_id=1,
            approval_draft_revision=1,
        )
    return MealItemInput(**values)


async def test_third_of_batch_is_rational_not_rounded_ingredient_mass(store, food):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        part = recipe_portions(recipe, 1000)[0]
        assert part.edible_milligrams == 100000
        assert part.recipe_share.fraction == Fraction(1, 3)
        await validate_recipe_item(connection, part)
        meal = await create(connection, food, items=(meal_input(part),))
    item = meal.items[0]
    assert item.edible_milligrams == 100000
    assert item.recipe_share == part.recipe_share
    values = {n.code: n.amount_scaled for n in item.nutrients}
    assert values["energy"] == 66666667
    assert values["protein"] == 3333333
    assert values["sodium"] == 0 and values["vitamin_d"] is None
    assert "potassium" not in values
    assert item.calculation_version.startswith("recipe-consumed-rational-micro-v1/")
    assert str(ingredient_grams(part.recipe_share, 100000)).startswith("33.333333333333333333")


async def test_tiny_exact_share_below_one_milligram_is_not_clamped(store, food):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food, mass=1, unit="g", total=100000)
        part = recipe_portions(recipe, 1)[0]
        meal = await create(connection, food, items=(meal_input(part),))
    assert part.recipe_share.fraction == Fraction(1, 100000)
    assert ingredient_grams(part.recipe_share, 1) == Decimal("0.00000001")
    assert {n.code: n.amount_scaled for n in meal.items[0].nutrients}["energy"] == 0


async def test_half_even_nutrient_rounding_handles_exact_half_quantum(store):
    async with store.write() as connection:
        food = await publish_reviewed_food(
            connection,
            reviewed_food(
                basis_grams="100",
                nutrients=[{"code": "energy", "amount": "0.000003", "unit": "kcal"}],
            ),
        )
        recipe = await save_recipe(connection, food, total=2000)
        part = recipe_portions(recipe, 1000)[0]
        meal = await create(connection, food, items=(meal_input(part),))
    assert meal.items[0].nutrients[0].amount_scaled == 2


async def test_recipe_versions_pin_foods_and_previous_definitions(store, settings, food):
    async with store.write() as connection:
        original = await save_recipe(connection, food)
        refreshed = await publish_reviewed_food(
            connection, reviewed_food(name="Refreshed synthetic source"), food_id=food.food_id
        )
        await action(connection, "change")
        changed = await revise_recipe(
            connection,
            original.id,
            original.version_id,
            action_key="change",
            definition=definition(refreshed, mass=200000, total=4000),
        )
        assert changed.version_number == 2
        assert await get_recipe_version(connection, original.version_id) == original
        assert await get_recipe_by_number(connection, original.id, 1) == original
        assert await get_recipe(connection, original.id) == changed
        assert await get_recipe_by_number(connection, original.id, 99) is None
        old_portion = recipe_portions(original, 1000)[0]
        await validate_recipe_item(connection, old_portion)
        meal = await create(connection, food, items=(meal_input(old_portion),))
    reopened = Store(settings)
    try:
        async with reopened.engine.connect() as connection:
            assert await get_recipe_version(connection, original.version_id) == original
            assert await get_meal(connection, meal.id) == meal
    finally:
        await reopened.close()


async def test_archive_restore_and_stale_edits_do_not_rewrite_history(store, food):
    async with store.write() as connection:
        first = await save_recipe(connection, food)
        await action(connection, "archive")
        archived = await revise_recipe(
            connection, first.id, first.version_id, action_key="archive", archived=True
        )
        assert archived.archived and archived.version_number == 2
        assert await list_recipes(connection) == ()
        assert await list_recipes(connection, include_archived=True) == (archived,)
        await action(connection, "stale")
        with pytest.raises(RecipeError, match="changed"):
            await revise_recipe(
                connection, first.id, first.version_id, action_key="stale", archived=False
            )
        await action(connection, "restore")
        restored = await revise_recipe(
            connection, first.id, archived.version_id, action_key="restore", archived=False
        )
        assert restored.version_number == 3 and not restored.archived
        assert await get_recipe_version(connection, first.version_id) == first
        await action(connection, "duplicate")
        with pytest.raises(RecipeError, match="already exists"):
            await create_recipe(
                connection, first.name.upper(), first.definition, action_key="duplicate"
            )


@pytest.mark.parametrize(
    ("ingredient_basis", "yield_basis", "portion_basis"),
    [
        ("Approximate ingredient", None, None),
        (None, "Approximate cooked yield", None),
        (None, None, "Approximate eaten portion"),
        ("Ingredient", "Yield", "Portion"),
    ],
)
async def test_each_uncertainty_requires_fresh_approval(
    store, food, ingredient_basis, yield_basis, portion_basis
):
    async with store.write() as connection:
        recipe = await save_recipe(
            connection, food, ingredient_basis=ingredient_basis, yield_basis=yield_basis
        )
        part = recipe_portions(recipe, 1000, estimate_basis=portion_basis)[0]
        assert part.estimate_basis
        await validate_recipe_item(connection, part)
        forged = replace(
            meal_input(part, approval_key="callback:approve"),
            quantity_method="measured",
            quantity_basis=None,
            approval_action_key=None,
            approved_at=None,
            approval_draft_id=None,
            approval_draft_revision=None,
        )
        with pytest.raises(MealError, match="uncertainty"):
            await create(connection, food, action_key="unapproved", items=(forged,))
        saved = await create(
            connection,
            food,
            action_key="callback:approve",
            items=(meal_input(part, approval_key="callback:approve"),),
        )
        assert saved.items[0].quantity_method == "approved_estimate"
        assert saved.items[0].recipe_share == part.recipe_share


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "recipe_id",
        "version_number",
        "total_units",
        "unit",
        "ingredient_index",
        "yield_estimate_basis",
    ],
)
async def test_forged_recipe_context_rejected_by_storage_and_ledger(store, food, field):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        part = recipe_portions(recipe, 1000)[0]
        values = dict(
            name="Forged",
            recipe_id=999,
            version_number=999,
            total_units=4000,
            unit="g",
            ingredient_index=1,
            yield_estimate_basis="Fake yield",
        )
        forged_share = part.recipe_share.model_copy(update={field: values[field]})
        forged = part.model_copy(update={"recipe_share": forged_share})
        with pytest.raises(RecipeError):
            await validate_recipe_item(connection, forged)
        with pytest.raises(MealError):
            await create(connection, food, items=(meal_input(forged),))


async def test_forged_food_or_batch_mass_cannot_change_recipe_portion(store, food):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        part = recipe_portions(recipe, 1000)[0]
        wrong_mass = part.model_copy(
            update={"edible_milligrams": 200000, "original_quantity": "200"}
        )
        with pytest.raises(RecipeError):
            await validate_recipe_item(connection, wrong_mass)
        other = await publish_reviewed_food(connection, reviewed_food(name="Other source"))
        with pytest.raises(RecipeError):
            await validate_recipe_item(
                connection, part.model_copy(update={"food_version_id": other.version_id})
            )


async def test_recipe_context_survives_date_delete_undo_and_unrelated_changes(store, food):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        part = recipe_portions(recipe, 1000)[0]
        first = await create(connection, food, items=(meal_input(part),))
        await action(connection, "date")
        moved = await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="date",
            local_date=date(2024, 1, 14),
            consumed_at=WHEN - 86400,
        )
        assert moved.items == first.items
        await action(connection, "delete")
        deleted = await revise_meal(
            connection,
            moved.id,
            moved.revision_id,
            action_key="delete",
            deleted=True,
            operation="delete",
        )
        await action(connection, "undo")
        restored = await undo_meal(connection, deleted.id, deleted.revision_id, action_key="undo")
        assert restored.items == first.items
        assert input_from_snapshot(restored.items[0]).recipe_share == part.recipe_share


@pytest.mark.parametrize("table", [recipes, recipe_versions, recipe_ingredients])
async def test_recipe_history_cannot_be_replaced(store, food, table):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.exec_driver_sql(
                    f"INSERT OR REPLACE INTO {table.name} SELECT * FROM {table.name}"
                )
        assert await get_recipe(connection, recipe.id) == recipe


@pytest.mark.parametrize(
    "mutation",
    [
        "ingredient",
        "version",
        "identity",
        "delete-version",
        "delete-ingredient",
        "rollback-pointer",
    ],
)
async def test_recipe_history_and_identity_are_guarded(store, food, mutation):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        statements = {
            "ingredient": sa.update(recipe_ingredients).values(edible_milligrams=1),
            "version": sa.update(recipe_versions).values(total_units=1),
            "identity": sa.update(recipes).values(name="Changed"),
            "delete-version": sa.delete(recipe_versions),
            "delete-ingredient": sa.delete(recipe_ingredients),
            "rollback-pointer": sa.update(recipes).values(current_version_id=None),
        }
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(statements[mutation])
        assert await get_recipe(connection, recipe.id) == recipe


async def test_food_and_recipe_versions_remain_pinned_by_history(store, food):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        for statement in (sa.delete(food_versions),):
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(statement)
        meal = await create(connection, food, items=(meal_input(recipe_portions(recipe, 1000)[0]),))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(sa.delete(recipes))
        await connection.execute(sa.delete(meals).where(meals.c.id == meal.id))
        await connection.execute(sa.delete(recipes))
        await connection.execute(sa.delete(food_versions))
        assert (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all() == []


@pytest.mark.parametrize(
    "name", ["", "a" * 61, "a=b", "a|b", "R1", "r2v3", "a\nb", "a\u2028b", "today stew"]
)
def test_recipe_names_reject_ambiguous_or_unsafe_forms(name):
    with pytest.raises(RecipeError):
        normalize_recipe_name(name)


def test_recipe_names_normalize_unicode_and_spacing():
    assert normalize_recipe_name("  CAFE\u0301   Stew ") == "café stew"


@pytest.mark.parametrize("portion", [0, -1, True, 3001, 1.5])
async def test_portion_outside_defined_batch_is_rejected(store, food, portion):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
    with pytest.raises(RecipeError):
        recipe_portions(recipe, portion)


async def test_scaling_preserves_rational_context_and_batch_ingredient_mass(store, food):
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
    item = recipe_portions(recipe, 1000)[0]
    doubled = item.recipe_share.scaled(Decimal(2))
    assert doubled.portion_units == 2000 and doubled.fraction == Fraction(2, 3)
    assert doubled.total_units == 3000
    assert doubled.amount_text() == "2 serving(s)"
    for factor in (Decimal("0.0001"), Decimal(4), Decimal("NaN"), 1.1):
        with pytest.raises(RecipeError):
            item.recipe_share.scaled(factor)


async def test_definition_rejects_unresolved_or_nested_recipe_ingredients(store, food):
    with pytest.raises(ValidationError):
        RecipeDefinition(
            items=(PlannedItem(food_version_id=food.version_id),), unit="serving", total_units=1000
        )
    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
    with pytest.raises(ValidationError):
        RecipeDefinition(items=recipe_portions(recipe, 1000), unit="serving", total_units=1000)
    with pytest.raises(ValidationError):
        definition(food, total=1_000_001)


async def test_populated_upgrade_downgrade_preserves_ordinary_meal_data_and_guards(settings):
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    await asyncio.to_thread(command.upgrade, config, "0010_recipe_shares")
    store = Store(settings)
    try:
        async with store.write() as connection:
            food = await publish_reviewed_food(connection, reviewed_food())
            await create(connection, food)
    finally:
        await store.close()
    await asyncio.to_thread(command.downgrade, config, "0008_favorites")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        before = {
            name: connection.execute(f"SELECT * FROM {name}").fetchall()
            for name in (
                "foods",
                "food_versions",
                "meals",
                "meal_revisions",
                "meal_items",
                "meal_item_nutrients",
            )
        }
        guards = dict(
            connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        )
    await asyncio.to_thread(command.upgrade, config, "0010_recipe_shares")
    store = Store(settings)
    try:
        async with store.write() as connection:
            await save_recipe(connection, food)
    finally:
        await store.close()
    await asyncio.to_thread(command.downgrade, config, "0008_favorites")
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert {
            name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in before
        } == before
        after = dict(
            connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        )
        assert after == guards
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_downgrade_refuses_to_erase_consumed_recipe_meaning(settings):
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.attributes["database_url"] = settings.resolved_database_url
    await asyncio.to_thread(command.upgrade, config, "0010_recipe_shares")
    store = Store(settings)
    try:
        async with store.write() as connection:
            food = await publish_reviewed_food(connection, reviewed_food())
            recipe = await save_recipe(connection, food)
            meal = await create(
                connection, food, items=(meal_input(recipe_portions(recipe, 1000)[0]),)
            )
    finally:
        await store.close()
    with pytest.raises(RuntimeError, match="Recipe provenance exists"):
        await asyncio.to_thread(command.downgrade, config, "0009_recipes")
    store = Store(settings)
    try:
        async with store.engine.connect() as connection:
            assert await get_meal(connection, meal.id) == meal
            assert (
                await connection.scalar(sa.select(meal_items.c.recipe_version_id))
                == recipe.version_id
            )
    finally:
        await store.close()


async def test_ingredient_uncertainty_does_not_mark_other_precise_ingredients_rough(store, food):
    async with store.write() as connection:
        await action(connection, "recipe")
        recipe = await create_recipe(
            connection,
            "Mixed certainty",
            RecipeDefinition(
                items=(ingredient(food, basis="Rough ingredient"), ingredient(food, 200000)),
                unit="g",
                total_units=250000,
            ),
            action_key="recipe",
        )
        portions = recipe_portions(recipe, 50000)
        assert portions[0].estimate_basis and portions[1].estimate_basis is None
        meal = await create(
            connection,
            food,
            action_key="callback:approve",
            items=tuple(meal_input(item, approval_key="callback:approve") for item in portions),
        )
    assert [item.quantity_method for item in meal.items] == ["approved_estimate", "measured"]


async def test_favorite_keeps_exact_recipe_share_after_source_meal_erasure(store, food):
    from nutrition_bot.adapters.database.favorites import create_favorite, get_favorite
    from nutrition_bot.domain.reuse import scale_items

    async with store.write() as connection:
        recipe = await save_recipe(connection, food)
        meal = await create(connection, food, items=(meal_input(recipe_portions(recipe, 1000)[0]),))
        await action(connection, "favorite")
        favorite = await create_favorite(connection, "Usual recipe", meal, action_key="favorite")
        assert favorite.items[0].recipe_share.fraction == Fraction(1, 3)
        assert scale_items(favorite.items, Decimal(2))[0].recipe_share.fraction == Fraction(2, 3)
        await connection.execute(sa.delete(meals))
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(sa.delete(recipes))
        assert await get_favorite(connection, favorite.id) == favorite


async def test_temporary_recipe_draft_pins_version_until_expiry(store, food):
    from nutrition_bot.adapters.database.drafts import create_draft, expire_drafts
    from nutrition_bot.adapters.database.schema_recipes import draft_recipe_refs
    from nutrition_bot.domain.drafts import DRAFT_TTL_SECONDS, DraftContent

    async with store.write() as connection:
        recipe = await save_recipe(connection, food, yield_basis="Approximate yield")
        await action(connection, "draft")
        draft = await create_draft(
            connection,
            DraftContent(
                label="Recipe draft",
                local_date=date(2024, 1, 15),
                timezone="UTC",
                consumed_at=WHEN,
                source_chat_id=101,
                source_message_id=30,
                items=recipe_portions(recipe, 1000),
            ),
            action_key="draft",
            now=WHEN,
        )
        assert (
            await connection.scalar(sa.select(draft_recipe_refs.c.recipe_version_id))
            == recipe.version_id
        )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(sa.delete(recipes))
        assert await expire_drafts(connection, now=WHEN + DRAFT_TTL_SECONDS) == 1
        assert (
            await connection.scalar(sa.select(sa.func.count()).select_from(draft_recipe_refs)) == 0
        )
        await connection.execute(sa.delete(recipes))
        assert draft.content.items[0].recipe_share.version_id == recipe.version_id
