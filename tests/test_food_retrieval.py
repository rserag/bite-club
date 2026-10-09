"""Synthetic catalog growth and relevant AI-context regressions."""

from nutrition_bot.adapters.database.aliases import create_alias
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.application.ai_service import AiService
from tests.test_food_storage import reviewed_food
from tests.test_meal_ledger import action


async def test_old_relevant_food_survives_more_than_forty_new_unrelated_foods(store):
    async with store.write() as connection:
        wanted = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic carrot, raw")
        )
        for index in range(45):
            await publish_reviewed_food(
                connection, reviewed_food(name=f"Synthetic unrelated product {index}")
            )
    context = await AiService(store)._catalog("I ate 80g raw carrot and 50g another food")
    assert len(context) == 40
    assert context[0].food_version_id == wanted.version_id


async def test_current_revision_replaces_history_without_crowding_out_other_identities(store):
    async with store.write() as connection:
        first = await publish_reviewed_food(connection, reviewed_food(name="Synthetic carrot, raw"))
        current = await publish_reviewed_food(
            connection,
            reviewed_food(name="Synthetic revised carrot, raw"),
            food_id=first.food_id,
        )
        unrelated = await publish_reviewed_food(connection, reviewed_food(name="Synthetic bean"))
    context = await AiService(store)._catalog("carrot")
    assert {item.food_version_id for item in context} == {
        current.version_id,
        unrelated.version_id,
    }
    assert context[0].food_version_id == current.version_id


async def test_explicit_active_alias_can_retrieve_its_pinned_historical_snapshot(store):
    async with store.write() as connection:
        first = await publish_reviewed_food(connection, reviewed_food(name="Synthetic carrot, raw"))
        await action(connection, "synthetic-retrieval-alias")
        await create_alias(
            connection,
            "Crunchy snack",
            first.version_id,
            action_key="synthetic-retrieval-alias",
        )
        current = await publish_reviewed_food(
            connection,
            reviewed_food(name="Synthetic revised carrot, raw"),
            food_id=first.food_id,
        )
    context = await AiService(store)._catalog("80g crunchy snack")
    assert context[0].food_version_id == first.version_id
    assert "Crunchy snack" in context[0].name
    assert current.version_id in {item.food_version_id for item in context}


async def test_unicode_words_and_literal_wildcards_do_not_change_query_semantics(store):
    async with store.write() as connection:
        wanted = await publish_reviewed_food(
            connection, reviewed_food(name="Synthetic Café cheese")
        )
        for index in range(41):
            await publish_reviewed_food(
                connection, reviewed_food(name=f"Synthetic unrelated {index}")
            )
    context = await AiService(store)._catalog("80g CAFE\u0301 % _")
    assert context[0].food_version_id == wanted.version_id
