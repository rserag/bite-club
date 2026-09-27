"""Explicit meal syntax cannot be shadowed by an implicit favorite name."""

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.favorites import list_favorites
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema_drafts import meal_drafts
from tests.helpers import message
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import current, food_record, ledger_counts, process
from tests.test_telegram_reuse import favorite, press


@pytest.mark.parametrize("text", ["150g rice", "I ate 150g rice"])
async def test_measured_favorite_name_cannot_override_a_measured_food_log(
    service, store, catalog, text
):
    await favorite(service, store, source="300g chicken", name="150g rice")
    response = await process(service, store, message(3, text))
    assert "Saved" in response["payload"]["text"]
    meal = await current(store, 2)
    assert len(meal.items) == 1
    assert meal.items[0].food_version_id == catalog["rice"].version_id
    assert meal.items[0].edible_milligrams == 150_000
    assert meal.items[0].quantity_method == "measured"


@pytest.mark.parametrize("text", ["about 150g rice", "I ate about 150g rice"])
async def test_approximate_favorite_name_still_requires_a_new_draft_approval(
    service, store, catalog, text
):
    await favorite(service, store, source="300g chicken", name="about 150g rice")
    response = await process(service, store, message(3, text))
    assert await ledger_counts(store) == (1, 1)
    assert "draft_id" in response["payload"]
    assert "rice" in response["payload"]["text"]
    assert "150" in response["payload"]["text"]
    assert "Approve estimate" in {item["text"] for item in response["payload"]["buttons"]}


@pytest.mark.parametrize(
    "name",
    [
        "probably rice",
        "3 eggs",
        "150ml milk",
        "rice plus oil",
        "rice ~150g",
        "rice approximately 150g",
        "150g rice approx",
        "150ish g rice",
        "rice and chicken",
    ],
)
async def test_qualifiers_or_unsupported_quantities_cannot_consume_a_favorite_implicitly(
    service, store, catalog, name
):
    await favorite(service, store, source="300g chicken", name=name)
    response = await process(service, store, message(3, f"I ate {name}"))
    assert response is not None
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize("name", ["150g rice", "about 150g rice", "3 eggs", "150ish g rice"])
async def test_explicit_eat_can_select_a_named_favorite_containing_meal_syntax(
    service, store, catalog, name
):
    await favorite(service, store, source="300g chicken", name=name)
    response = await process(service, store, message(3, f"/eat {name}"))
    assert "Saved" in response["payload"]["text"]
    assert await ledger_counts(store) == (2, 2)
    meal = await current(store, 2)
    assert meal.items[0].food_version_id == catalog["chicken"].version_id
    assert meal.items[0].edible_milligrams == 300_000


@pytest.mark.parametrize("huge", [str(2**63), "9" * 1000])
@pytest.mark.parametrize(
    "command",
    [
        "/meal M{huge}",
        "/repeat M{huge}r1",
        "/favorite save lunch = M{huge}r1",
        "/eat F{huge}v1",
        "/alias A{huge}",
    ],
)
async def test_oversized_references_are_rejected_before_database_integer_binding(
    service, store, catalog, huge, command
):
    response = await process(service, store, message(1, command.format(huge=huge)))
    assert response is not None
    assert "Nothing was changed" in response["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


@pytest.mark.parametrize("name", ["today breakfast", "yesterday breakfast", "2023-11-12 breakfast"])
async def test_favorite_creation_rejects_names_shadowed_by_eat_date_syntax(
    service, store, catalog, name
):
    await process(service, store, message(1, "150g rice"))
    response = await process(service, store, message(2, f"/favorite save {name} = M1r1"))
    assert "Nothing was changed" in response["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert await list_favorites(connection) == ()


async def test_repeated_tap_with_distinct_callback_id_does_not_duplicate_measured_consumption(
    service, store, catalog
):
    _, preview = await favorite(service, store)
    await process(service, store, press(preview, update_id=3, callback_id="first-tap"))
    original = await current(store, 2)
    response = await process(service, store, press(preview, update_id=4, callback_id="second-tap"))
    assert "Nothing was changed" in response["payload"]["text"]
    assert await ledger_counts(store) == (2, 2)
    assert await current(store, 2) == original


async def test_repeated_tap_with_distinct_callback_id_does_not_duplicate_estimate_draft(
    service, store, catalog
):
    proposal = await process(service, store, message(1, "about 150g rice"))
    await process(service, store, approve_draft(proposal, update_id=2))
    preview = await process(service, store, message(3, "/favorite save usual rice = M1r1"))
    first = await process(service, store, press(preview, update_id=4, callback_id="first-tap"))
    assert "draft_id" in first["payload"]
    second = await process(service, store, press(preview, update_id=5, callback_id="second-tap"))
    assert "Nothing was changed" in second["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)
    async with store.engine.connect() as connection:
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_drafts)) == 2


async def test_food_choices_fit_telegram_with_maximum_supplementary_unicode_labels(service, store):
    # Supplementary alphabetic characters use two UTF-16 units but are valid names.
    async with store.write() as connection:
        foods = [
            await publish_reviewed_food(connection, food_record("𝓐" * 499 + str(index)))
            for index in range(6)
        ]
    for index in range(10):
        name = "𝓑" * 79 + chr(65 + index)
        response = await process(
            service,
            store,
            message(index + 1, f"/alias {name} = #{foods[0].version_id}"),
        )
        assert "Nothing was logged" in response["payload"]["text"]
    response = await process(service, store, message(11, "/foods"))
    text = response["payload"]["text"]
    assert sum(line.startswith("#") for line in text.splitlines()) == 6
    assert sum(line.startswith("Alias ") for line in text.splitlines()) == 3
    assert "/aliases" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096
