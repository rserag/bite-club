from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.meals import MealItemInput, revise_meal, undo_meal
from nutrition_bot.application.portion_suggestions import suggest_portion
from tests.test_food_storage import reviewed_food
from tests.test_meal_ledger import WHEN, action, create, measured


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food(portions=[]))


async def log(connection, food, number, grams, *, items=None, estimated=False, when=None):
    key = f"callback:log-{number}" if estimated else f"log-{number}"
    if items is None:
        items = (estimate(food, grams, key, number),) if estimated else (measured(food, grams),)
    return await create(
        connection,
        food,
        items=items,
        action_key=key,
        source_message_id=number,
        consumed_at=WHEN + number if when is None else when,
    )


def estimate(food, grams, key, number):
    return MealItemInput(
        food.version_id,
        int(Decimal(grams) * 1000),
        grams,
        "g",
        quantity_method="approved_estimate",
        quantity_basis="Synthetic user-approved estimate",
        approval_action_key=key,
        approved_at=WHEN,
        approval_draft_id=number,
        approval_draft_revision=1,
    )


async def test_no_qualifying_history_or_portion_means_no_invented_default(store, food):
    async with store.write() as connection:
        assert await suggest_portion(connection, food.version_id) is None
        await log(connection, food, 1, "100")
        await log(connection, food, 2, "200")
        assert await suggest_portion(connection, food.version_id) is None
        for invalid in (0, -1, 2**63, 99999, True, "1"):
            assert await suggest_portion(connection, invalid) is None


async def test_history_uses_median_of_distinct_meals_and_reports_count(store, food):
    async with store.write() as connection:
        for number, grams in enumerate(("150", "900", "200"), 1):
            await log(connection, food, number, grams)
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 200_000
    assert result.basis == "History estimate: median of 3 measured meals for this food version"
    with pytest.raises(FrozenInstanceError):
        result.edible_milligrams = 1


async def test_history_uses_only_last_ten_consumed_meals_with_stable_tie_break(store, food):
    async with store.write() as connection:
        # Identical consumption timestamps make the meal ID the recency tie-break.
        for number in range(1, 13):
            await log(connection, food, number, str(number * 10), when=WHEN)
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 75_000
    assert "10 measured meals" in result.basis


async def test_history_recency_is_consumed_time_not_creation_time(store, food):
    async with store.write() as connection:
        for number in range(1, 11):
            await log(connection, food, number, "100")
        await log(connection, food, 11, "900", when=WHEN - 1)
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 100_000


@pytest.mark.parametrize(
    "amounts,expected",
    [
        (("100", "101", "102", "103"), 102_000),
        (("100.001", "100.499", "100.999"), 100_000),
        (("0.001", "0.002", "0.003"), 1),
    ],
)
async def test_history_rounding_is_deterministic_and_never_zero(store, food, amounts, expected):
    async with store.write() as connection:
        for number, grams in enumerate(amounts, 1):
            await log(connection, food, number, grams)
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == expected


async def test_duplicates_are_summed_per_meal_and_cannot_supply_three_observations(store, food):
    async with store.write() as connection:
        await log(connection, food, 1, "0", items=(measured(food, "30"), measured(food, "70")))
        assert await suggest_portion(connection, food.version_id) is None
        await log(connection, food, 2, "200")
        assert await suggest_portion(connection, food.version_id) is None
        await log(connection, food, 3, "300")
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 200_000
    assert "3 measured meals" in result.basis


async def test_only_current_revision_participates_and_deleted_meals_are_excluded(store, food):
    async with store.write() as connection:
        first = await log(connection, food, 1, "1000")
        await log(connection, food, 2, "200")
        third = await log(connection, food, 3, "300")
        await action(connection, "correct-first")
        await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="correct-first",
            items=(measured(food, "100"),),
        )
        assert (await suggest_portion(connection, food.version_id)).edible_milligrams == 200_000
        await action(connection, "delete-third")
        deleted = await revise_meal(
            connection,
            third.id,
            third.revision_id,
            action_key="delete-third",
            operation="delete",
        )
        assert await suggest_portion(connection, food.version_id) is None
        await action(connection, "restore-third")
        await undo_meal(connection, third.id, deleted.revision_id, action_key="restore-third")
        assert (await suggest_portion(connection, food.version_id)).edible_milligrams == 200_000


async def test_approved_estimates_and_meals_mixing_them_with_measured_amounts_are_excluded(
    store, food
):
    async with store.write() as connection:
        await log(connection, food, 1, "100")
        await log(connection, food, 2, "200")
        await log(connection, food, 3, "500", estimated=True)
        await log(
            connection,
            food,
            4,
            "0",
            estimated=True,
            items=(measured(food, "100"), estimate(food, "900", "callback:log-4", 4)),
        )
        assert await suggest_portion(connection, food.version_id) is None
        await log(connection, food, 5, "300")
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 200_000
    assert "3 measured meals" in result.basis


async def test_correction_to_estimate_invalidates_former_measured_history(store, food):
    async with store.write() as connection:
        first = await log(connection, food, 1, "100")
        await log(connection, food, 2, "200")
        await log(connection, food, 3, "300")
        await action(connection, "callback:correction")
        await revise_meal(
            connection,
            first.id,
            first.revision_id,
            action_key="callback:correction",
            items=(estimate(food, "100", "callback:correction", 4),),
        )
        assert await suggest_portion(connection, food.version_id) is None


async def test_history_does_not_cross_food_versions_or_preparations(store, food):
    async with store.write() as connection:
        second_version = await publish_reviewed_food(
            connection,
            reviewed_food(name="Synthetic renamed food", portions=[]),
            food_id=food.food_id,
        )
        cooked = await publish_reviewed_food(
            connection, reviewed_food(name=food.record.name, preparation="cooked", portions=[])
        )
        await log(connection, food, 1, "100")
        await log(connection, second_version, 2, "200")
        await log(connection, cooked, 3, "300")
        for version in (food, second_version, cooked):
            assert await suggest_portion(connection, version.version_id) is None


async def test_single_catalog_portion_is_an_estimate_with_source_even_if_marked_measured(store):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, reviewed_food())
        await log(connection, food, 1, "100")
        await log(connection, food, 2, "200")
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 75_125
    assert result.basis == (
        "Catalog estimate: Synthetic portion; source: Synthetic weighing record"
    )


async def test_history_takes_priority_over_single_catalog_portion(store):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, reviewed_food())
        for number in range(1, 4):
            await log(connection, food, number, "100")
        result = await suggest_portion(connection, food.version_id)
    assert result.edible_milligrams == 100_000
    assert result.basis.startswith("History estimate:")


async def test_multiple_catalog_portions_require_user_choice(store):
    portion = reviewed_food().portions[0].model_dump(mode="json")
    async with store.write() as connection:
        food = await publish_reviewed_food(
            connection, reviewed_food(portions=[portion, {**portion, "label": "Second portion"}])
        )
        assert await suggest_portion(connection, food.version_id) is None


async def test_catalog_basis_is_bounded_readable_and_keeps_label_and_source(store):
    portion = reviewed_food().portions[0].model_dump(mode="json")
    async with store.write() as connection:
        food = await publish_reviewed_food(
            connection,
            reviewed_food(
                portions=[
                    {
                        **portion,
                        "label": "Label\n" + "a" * 400,
                        "source": "Source\u202e" + "b" * 400,
                    }
                ]
            ),
        )
        result = await suggest_portion(connection, food.version_id)
    assert len(result.basis) <= 300
    assert "Label " in result.basis and "source: Source " in result.basis
    assert "\n" not in result.basis and "\u202e" not in result.basis


async def test_unrepresentably_large_history_or_catalog_portions_are_not_suggested(store, food):
    async with store.write() as connection:
        for number in range(1, 4):
            await log(
                connection,
                food,
                number,
                "0",
                items=(measured(food, "50000"), measured(food, "50000")),
            )
        assert await suggest_portion(connection, food.version_id) is None
        portion = reviewed_food().portions[0].model_dump(mode="json")
        large = await publish_reviewed_food(
            connection, reviewed_food(portions=[{**portion, "grams": "50000.001"}])
        )
        assert await suggest_portion(connection, large.version_id) is None
