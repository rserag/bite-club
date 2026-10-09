"""Synthetic Mini App recipe regression: batch masses never become consumed masses."""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.schema import inbox, meal_revisions
from tests.helpers import message
from tests.test_miniapp_server import client as client
from tests.test_miniapp_server import headers
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, process
from tests.test_telegram_recipes import definition, nutrient


async def saved_recipe(service, store, catalog, *, basis="yield 1200g", portion="300g"):
    await definition(service, store, catalog, basis=basis)
    await process(service, store, message(2, f"/recipe log R1v1 {portion}"))
    return await current(store)


async def history(client):
    return (await (await client.get("/api/meals", headers=headers())).json())["meals"][0]


async def test_history_and_unchanged_edit_preserve_quarter_batch(client, service, store, catalog):
    before = await saved_recipe(service, store, catalog)
    exposed = await history(client)
    assert [item["grams"] for item in exposed["items"]] == ["125", "5"]
    assert all(item["recipe_ingredient"] for item in exposed["items"])
    assert [item["quantity_text"] for item in exposed["items"]] == [
        "125.000 g equivalent",
        "5.000 g equivalent",
    ]
    assert exposed["recipes"][0]["amount"] == "300"
    assert exposed["recipes"][0]["fraction"] == "1/4"
    assert exposed["recipe_editable"]
    unchanged = {
        "request_id": str(uuid4()),
        "action": "edit",
        "reference": exposed["reference"],
        "items": [
            {"version_id": item["version_id"], "grams": item["grams"]} for item in exposed["items"]
        ],
    }
    response = await client.post("/api/commands", json=unchanged, headers=headers())
    assert response.status == 409
    assert "recipe" in (await response.json())["error"].lower()
    assert (await current(store)).revision_id == before.revision_id
    assert sum(nutrient(item, "energy") for item in before.items) == 135000000
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(inbox)) == 2


@pytest.mark.parametrize(
    ("basis", "portion", "unit", "value"),
    [
        ("yield 1200g", "300g", "g", "300"),
        ("servings 4", "1 serving", "serving", "1"),
    ],
)
async def test_true_recipe_portion_edit_pins_original_batch_and_is_idempotent(
    client, service, store, catalog, basis, portion, unit, value
):
    before = await saved_recipe(service, store, catalog, basis=basis, portion=portion)
    await process(
        service,
        store,
        message(3, f"/recipe update R1v1 = 900g #{catalog['rice'].version_id} | yield 1000g"),
    )
    data = {
        "request_id": str(uuid4()),
        "action": "recipe_portion",
        "reference": f"M1r{before.revision_number}",
        "portion": value,
        "unit": unit,
    }
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 200
    assert await service.process_one()
    assert not await service.process_one()
    after = await current(store)
    assert after.items == before.items
    assert all(item.recipe_share.version_number == 1 for item in after.items)
    assert sum(nutrient(item, "energy") for item in after.items) == 135000000
    stale = data | {"request_id": str(uuid4()), "portion": "2"}
    assert (await client.post("/api/commands", json=stale, headers=headers())).status == 202
    assert await service.process_one()
    assert (await current(store)).revision_id == after.revision_id
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_revisions)) == 2


async def test_estimated_recipe_change_requires_fresh_telegram_approval(
    client, service, store, catalog
):
    before = await saved_recipe(service, store, catalog)
    data = {
        "request_id": str(uuid4()),
        "action": "recipe_portion",
        "reference": "M1r1",
        "portion": "250",
        "unit": "g",
        "estimated": True,
    }
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    assert (await current(store)).revision_id == before.revision_id
    receipt = await (
        await client.get("/api/requests/" + data["request_id"], headers=headers())
    ).json()
    assert receipt["replies"][0]["needs_approval"]
    assert "Recipe correction draft" in receipt["replies"][0]["text"]


async def test_explicit_recipe_detach_requires_selected_replacement_foods(
    client, service, store, catalog
):
    await saved_recipe(service, store, catalog)
    data = {
        "request_id": str(uuid4()),
        "action": "edit",
        "reference": "M1r1",
        "detach_recipe": True,
        "items": [
            {"version_id": catalog["rice"].version_id, "grams": "125"},
            {"version_id": catalog["chicken"].version_id, "grams": "5"},
        ],
    }
    assert (await client.post("/api/commands", json=data, headers=headers())).status == 202
    assert await service.process_one()
    saved = await current(store)
    assert all(item.recipe_share is None for item in saved.items)
    assert sum(nutrient(item, "energy") for item in saved.items) == 135000000
