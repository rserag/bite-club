"""Synthetic guided recipes preserve batch identity, exact shares and fresh consent."""

from fractions import Fraction

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.recipes import get_recipe
from nutrition_bot.adapters.database.schema_recipes import recipe_versions, recipes
from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.service import Service
from tests.helpers import message
from tests.test_navigation import tap
from tests.test_telegram_drafts import press as approve
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, process, reply
from tests.test_telegram_recipes import press as recipe_press


class Guide:
    def __init__(self, service, store):
        self.service, self.store, self.uid = service, store, 100

    async def send(self, text):
        self.uid += 1
        return await process(self.service, self.store, message(self.uid, text))

    async def tap(self, receipt, label):
        self.uid += 1
        return await process(self.service, self.store, tap(receipt, label, self.uid))

    async def ingredient(self, name, amount):
        choices = await self.send(name)
        label = next(
            b["text"]
            for b in choices["payload"]["buttons"]
            if name.casefold() in b["text"].casefold()
        )
        await self.tap(choices, label)
        return await self.send(amount)

    async def create(self, *, ingredient="500g", batch="1200g", servings=False):
        home = await self.send("/home")
        listing = await self.tap(home, "Recipes")
        await self.tap(listing, "Create recipe")
        await self.send("rice batch")
        ingredients = await self.ingredient("rice", ingredient)
        size = await self.tap(ingredients, "Set batch size")
        await self.tap(size, "Equal servings" if servings else "Cooked batch weight")
        review = await self.send(batch)
        saved = await self.tap(review, "Save recipe")
        return review, saved


async def recipe_count(store, table=recipes):
    async with store.engine.connect() as connection:
        return await connection.scalar(sa.select(sa.func.count()).select_from(table))


async def test_create_and_log_measured_batch_without_identifiers(service, store, catalog):
    guide = Guide(service, store)
    review, saved = await guide.create()
    assert "500 g rice" in review["payload"]["text"]
    assert "1200 g edible cooked yield" in review["payload"]["text"]
    assert "#" not in review["payload"]["text"]
    assert "Saved recipe" in saved["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await guide.tap(saved, "Log portion")
    receipt = await guide.send("300g")
    meal = await current(store)
    assert await ledger_counts(store) == (1, 1)
    assert meal.items[0].quantity_method == "measured"
    assert meal.items[0].food_version_id == catalog["rice"].version_id
    assert Fraction(
        meal.items[0].recipe_share.portion_units, meal.items[0].recipe_share.total_units
    ) == Fraction(1, 4)
    assert "125 kcal" in receipt["payload"]["text"]
    assert "unknown" in receipt["payload"]["text"]


@pytest.mark.parametrize("uncertain", ["ingredient", "batch", "portion"])
async def test_estimated_uses_require_new_current_revision_approval(
    service, store, catalog, uncertain
):
    guide = Guide(service, store)
    _, saved = await guide.create(
        ingredient="about 500g" if uncertain == "ingredient" else "500g",
        batch="about 1200g" if uncertain == "batch" else "1200g",
    )
    assert "Approve estimate" not in [b["text"] for b in saved["payload"]["buttons"]]
    await guide.tap(saved, "Log portion")
    draft = await guide.send("about 300g" if uncertain == "portion" else "300g")
    assert "draft_id" in draft["payload"]
    assert await ledger_counts(store) == (0, 0)
    await process(service, store, approve(draft, update_id=300, callback_id="guide-first"))
    listing = await guide.send("/recipes")
    opened = await guide.tap(listing, "rice batch")
    await guide.tap(opened, "Log portion")
    fresh = await guide.send("about 300g" if uncertain == "portion" else "300g")
    assert fresh["payload"]["draft_id"] != draft["payload"]["draft_id"]
    assert await ledger_counts(store) == (1, 1)
    updated = await process(service, store, reply(301, "portion about 250g", fresh))
    await process(service, store, approve(fresh, update_id=302, callback_id="guide-stale"))
    assert await ledger_counts(store) == (1, 1)
    await process(service, store, approve(updated, update_id=303, callback_id="guide-current"))
    meal = await current(store, 2)
    assert meal.items[0].approval_draft_revision == 2
    assert Fraction(
        meal.items[0].recipe_share.portion_units, meal.items[0].recipe_share.total_units
    ) == Fraction(5, 24)


async def test_new_batch_preserves_old_meals_and_explicit_selected_food_versions(
    service, store, catalog
):
    guide = Guide(service, store)
    _, saved = await guide.create()
    await guide.tap(saved, "Log portion")
    await guide.send("300g")
    original = await current(store)
    async with store.write() as connection:
        refreshed = await publish_reviewed_food(
            connection, food_record("rice", energy="999"), food_id=catalog["rice"].food_id
        )
    assert refreshed.version_id != original.items[0].food_version_id
    listing = await guide.send("/recipes")
    opened = await guide.tap(listing, "rice batch")
    ingredients = await guide.tap(opened, "New batch")
    size = await guide.tap(ingredients, "Set batch size")
    await guide.tap(size, "Cooked batch weight")
    review = await guide.send("1000g")
    updated = await guide.tap(review, "Save recipe")
    assert await recipe_count(store, recipe_versions) == 2
    await guide.tap(updated, "Log portion")
    await guide.send("300g")
    newer = await current(store, 2)
    assert await current(store) == original
    assert newer.items[0].food_version_id == original.items[0].food_version_id
    assert newer.items[0].recipe_share.version_id != original.items[0].recipe_share.version_id
    assert Fraction(
        newer.items[0].recipe_share.portion_units, newer.items[0].recipe_share.total_units
    ) == Fraction(3, 10)


async def test_stale_save_cannot_save_changed_batch(service, store, catalog):
    guide = Guide(service, store)
    review, _ = await guide.create()
    stale = await guide.tap(review, "Save recipe")
    assert "changed or expired" in stale["payload"]["text"]
    assert await recipe_count(store) == 1
    listing = await guide.send("/recipes")
    opened = await guide.tap(listing, "rice batch")
    ingredients = await guide.tap(opened, "New batch")
    size = await guide.tap(ingredients, "Set batch size")
    await guide.tap(size, "Cooked batch weight")
    old_review = await guide.send("1000g")
    size = await guide.tap(old_review, "Change batch size")
    await guide.tap(size, "Cooked batch weight")
    new_review = await guide.send("900g")
    await guide.tap(old_review, "Save recipe")
    assert await recipe_count(store, recipe_versions) == 1
    await guide.tap(new_review, "Save recipe")
    async with store.engine.connect() as connection:
        assert (await get_recipe(connection, 1)).definition.total_units == 900000


async def test_servings_reject_unprovided_gram_conversion_and_keep_flow_open(
    service, store, catalog
):
    guide = Guide(service, store)
    _, saved = await guide.create(batch="4", servings=True)
    await guide.tap(saved, "Log portion")
    rejected = await guide.send("250g")
    assert "grams and servings" in rejected["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await guide.send("1.5")
    meal = await current(store)
    assert Fraction(
        meal.items[0].recipe_share.portion_units, meal.items[0].recipe_share.total_units
    ) == Fraction(3, 8)


async def test_ingredient_remove_and_add_keeps_recipe_and_avoids_consumption(
    service, store, catalog
):
    guide = Guide(service, store)
    home = await guide.send("/home")
    listing = await guide.tap(home, "Recipes")
    await guide.tap(listing, "Create recipe")
    await guide.send("mixed batch")
    ingredients = await guide.ingredient("rice", "500g")
    await guide.tap(ingredients, "Add ingredient")
    ingredients = await guide.ingredient("chicken", "200g")
    ingredients = await guide.tap(ingredients, "Remove last ingredient")
    assert "chicken" not in ingredients["payload"]["text"]
    await guide.tap(ingredients, "Add ingredient")
    ingredients = await guide.ingredient("beans", "100g")
    assert "500 g rice" in ingredients["payload"]["text"]
    assert "100 g beans" in ingredients["payload"]["text"]
    assert await recipe_count(store) == 0
    assert await ledger_counts(store) == (0, 0)


async def test_portion_prompt_survives_restart_and_corrects_date_and_quantity(
    service, store, settings, catalog, monkeypatch
):
    monkeypatch.setattr("nutrition_bot.application.service.time.time", lambda: 1700000000)
    guide = Guide(service, store)
    _, saved = await guide.create()
    await guide.tap(saved, "Log portion")
    guide.service = Service(store, settings)
    receipt = await guide.send("yesterday 250g")
    meal = await current(store)
    assert meal.local_date.isoformat() == "2023-11-13"
    await process(service, store, reply(300, "portion 150g", receipt))
    revised = await current(store)
    assert Fraction(
        revised.items[0].recipe_share.portion_units, revised.items[0].recipe_share.total_units
    ) == Fraction(1, 8)
    assert revised.items[0].recipe_share.version_id == meal.items[0].recipe_share.version_id


async def test_existing_recipe_receipt_portion_button_opens_typed_guide(service, store, catalog):
    created = await process(
        service,
        store,
        message(1, f"/recipe create old batch = 500g #{catalog['rice'].version_id} | yield 1200g"),
    )
    prompt = await process(service, store, recipe_press(created, update_id=2))
    assert "How much old batch" in prompt["payload"]["text"]
    await process(service, store, message(3, "300g"))
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize("amount", ["0", "50001g", "0.0001g", "500g extra", "2 servings", "NaN"])
async def test_invalid_ingredient_mass_never_advances_or_invents_data(
    service, store, catalog, amount
):
    guide = Guide(service, store)
    listing = await guide.send("/recipes")
    await guide.tap(listing, "Create recipe")
    await guide.send("bad batch")
    choices = await guide.send("rice")
    await guide.tap(
        choices,
        next(b["text"] for b in choices["payload"]["buttons"] if "rice" in b["text"].casefold()),
    )
    response = await guide.send(amount)
    assert "still open" in response["payload"]["text"]
    async with store.engine.connect() as connection:
        row = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert row["stage"] == "recipe_amount"
        assert row["payload"]["ingredients"] == []
    assert await recipe_count(store) == 0
    assert await ledger_counts(store) == (0, 0)
