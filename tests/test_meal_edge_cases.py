import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import inbox, meals
from nutrition_bot.application.food_catalog import FoodCatalog
from nutrition_bot.application.service import Service
from tests.helpers import message
from tests.test_telegram_meals import current, food_record, process, reply


async def test_unicode_names_match_consistently_in_search_and_meal_logging(service, store):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, food_record("Яблоко"))
    found = await FoodCatalog(store, None).search("ЯБЛОКО", offline=True)
    assert found.local[0].version_id == food.version_id
    choices = await process(service, store, message(1, "/foods яблоко"))
    assert "Яблоко" in choices["payload"]["text"]
    logged = await process(service, store, message(2, "150g яблоко"))
    assert "Saved M1r1" in logged["payload"]["text"]
    assert (await current(store)).items[0].food_name == "Яблоко"


@pytest.mark.parametrize("correction", [False, True])
async def test_extreme_local_date_is_rejected_without_poisoning_inbox(store, settings, correction):
    service = Service(store, settings.model_copy(update={"app_timezone": "Pacific/Apia"}))
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, food_record("rice"))
    if correction:
        first = await process(service, store, message(1, "150g rice"))
        bad = reply(2, "date 0001-01-01", first)
    else:
        bad = message(2, f"0001-01-01 150g #{food.version_id}")
    rejected = await process(service, store, bad)
    assert "Nothing was changed" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(inbox.c.status).where(inbox.c.update_id == 2))
            == "done"
        )
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meals)) == int(
            correction
        )
    good = await process(service, store, message(3, "120g rice"))
    assert "Saved" in good["payload"]["text"]
