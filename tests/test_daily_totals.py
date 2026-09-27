from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.daily import daily_totals
from nutrition_bot.adapters.database.foods import publish_reviewed_food, register_nutrient
from nutrition_bot.adapters.database.meals import revise_meal, undo_meal
from nutrition_bot.adapters.database.schema import meal_revisions, profile
from nutrition_bot.domain.daily import CORE_NUTRIENT_ORDER
from nutrition_bot.domain.food import MAX_INTEGER, NutrientDefinition
from nutrition_bot.domain.food_source import SourceProvenance
from tests.test_food_storage import reviewed_food
from tests.test_meal_ledger import DAY, WHEN, action, create, measured


@pytest.fixture
async def food(store):
    async with store.write() as connection:
        return await publish_reviewed_food(connection, reviewed_food())


def values(totals):
    return {value.code: value for value in totals.nutrients}


async def test_unlogged_day_preserves_unknown_and_complete_registry(store):
    async with store.engine.connect() as connection:
        totals = await daily_totals(connection, DAY)
    assert totals.local_date == DAY
    assert totals.meals == ()
    assert totals.item_count == 0
    assert totals.source_counts == totals.quantity_counts == totals.recorded_timezones == ()
    assert tuple(value.code for value in totals.nutrients) == CORE_NUTRIENT_ORDER
    for nutrient in totals.nutrients:
        assert nutrient.known_amount_scaled is None
        assert nutrient.known_items == nutrient.total_items == nutrient.missing_items == 0
        assert nutrient.quality_counts == ()


async def test_scaled_totals_known_zero_partial_and_absent_values(store, food):
    async with store.write() as connection:
        other = await publish_reviewed_food(
            connection,
            reviewed_food(
                name="Synthetic other food",
                basis_grams="100",
                nutrients=[
                    {"code": "energy", "amount": "50", "unit": "kcal"},
                    {"code": "protein", "amount": None, "unit": "g"},
                    {"code": "vitamin_c", "amount": "3", "unit": "mg"},
                ],
            ),
        )
        meal = await create(
            connection, food, items=(measured(food, "125.125"), measured(other, "200"))
        )
        totals = await daily_totals(connection, DAY)
    assert [(meal.id, meal.revision_number, meal.label)] == [
        (value.id, value.revision_number, value.label) for value in totals.meals
    ]
    assert totals.item_count == 2
    result = values(totals)
    assert result["energy"].known_amount_scaled == 350_250_000
    assert result["energy"].known_items == 2
    assert result["energy"].missing_items == 0
    assert result["protein"].known_amount_scaled == 12_512_500
    assert result["protein"].known_items == result["protein"].missing_items == 1
    assert result["sodium"].known_amount_scaled == 0
    assert result["sodium"].known_items == result["sodium"].missing_items == 1
    assert result["vitamin_c"].known_amount_scaled == 6_000_000
    for code in ("vitamin_d", "potassium"):
        assert result[code].known_amount_scaled is None
        assert result[code].known_items == 0
        assert result[code].missing_items == 2
        assert result[code].quality_counts == ()
    assert totals.source_counts == (("manual_reviewed", 2),)
    assert totals.quantity_counts == (("measured", 2),)
    assert totals.recorded_timezones == ("UTC",)


async def test_repeated_food_entries_are_separate_coverage_items(store, food):
    async with store.write() as connection:
        await create(
            connection,
            food,
            items=(measured(food, "100"), measured(food, "200"), measured(food, "50")),
        )
        await create(connection, food, action_key="another", source_message_id=11)
        totals = await daily_totals(connection, DAY)
    assert len(totals.meals) == 2
    assert totals.item_count == 4
    assert totals.source_counts == (("manual_reviewed", 4),)
    assert totals.quantity_counts == (("measured", 4),)
    assert values(totals)["energy"].known_items == 4
    assert values(totals)["energy"].known_amount_scaled == 950_250_000
    assert values(totals)["energy"].quality_counts == (("manual_reviewed", 4),)


async def test_only_current_revision_counts_after_edit_delete_and_undo(store, food):
    async with store.write() as connection:
        original = await create(connection, food, items=(measured(food, "100"),))
        await action(connection, "edit")
        edited = await revise_meal(
            connection,
            original.id,
            original.revision_id,
            action_key="edit",
            label="Synthetic corrected",
            items=(measured(food, "200"),),
        )
        edited_totals = await daily_totals(connection, DAY)
        assert values(edited_totals)["energy"].known_amount_scaled == 400_000_000
        assert edited_totals.item_count == 1
        assert edited_totals.meals[0].revision_number == 2
        assert edited_totals.meals[0].label == "Synthetic corrected"
        await action(connection, "delete")
        deleted = await revise_meal(
            connection, original.id, edited.revision_id, action_key="delete", operation="delete"
        )
        empty = await daily_totals(connection, DAY)
        assert empty.meals == () and empty.item_count == 0
        assert values(empty)["energy"].known_amount_scaled is None
        await action(connection, "undo")
        restored = await undo_meal(connection, original.id, deleted.revision_id, action_key="undo")
        restored_totals = await daily_totals(connection, DAY)
        assert values(restored_totals)["energy"].known_amount_scaled == 400_000_000
        assert restored_totals.meals[0].revision_number == restored.revision_number == 4
        assert await connection.scalar(sa.select(sa.func.count()).select_from(meal_revisions)) == 4


async def test_date_correction_and_undo_move_exactly_one_current_meal(store, food):
    next_day = DAY + timedelta(days=1)
    async with store.write() as connection:
        original = await create(connection, food)
        before = await daily_totals(connection, DAY)
        await action(connection, "move")
        moved = await revise_meal(
            connection, original.id, original.revision_id, action_key="move", local_date=next_day
        )
        assert (await daily_totals(connection, DAY)).item_count == 0
        next_totals = await daily_totals(connection, next_day)
        assert next_totals.item_count == 1
        assert next_totals.nutrients == before.nutrients
        await action(connection, "undo")
        await undo_meal(connection, original.id, moved.revision_id, action_key="undo")
        assert (await daily_totals(connection, next_day)).item_count == 0
        assert (await daily_totals(connection, DAY)).nutrients == before.nutrients


async def test_refreshing_catalog_does_not_recompute_recorded_meals(store, food):
    async with store.write() as connection:
        await create(connection, food)
        before = await daily_totals(connection, DAY)
        refreshed = await publish_reviewed_food(
            connection,
            reviewed_food(nutrients=[{"code": "energy", "amount": "999", "unit": "kcal"}]),
            food_id=food.food_id,
        )
        assert refreshed.version_id != food.version_id
        assert await daily_totals(connection, DAY) == before


@pytest.mark.parametrize(
    ("stamp", "zone", "expected_date"),
    [
        ("2024-01-15T00:01:00+00:00", "America/New_York", date(2024, 1, 14)),
        ("2024-01-15T23:59:00+00:00", "Asia/Tokyo", date(2024, 1, 16)),
        ("2024-03-10T06:59:00+00:00", "America/New_York", date(2024, 3, 10)),
        ("2024-03-10T07:01:00+00:00", "America/New_York", date(2024, 3, 10)),
        ("2024-11-03T05:30:00+00:00", "America/New_York", date(2024, 11, 3)),
        ("2024-11-03T06:30:00+00:00", "America/New_York", date(2024, 11, 3)),
    ],
)
async def test_dates_remain_recorded_local_dates_across_midnight_and_dst(
    store, food, stamp, zone, expected_date
):
    async with store.write() as connection:
        await create(
            connection,
            food,
            timezone=zone,
            local_date=expected_date,
            consumed_at=datetime.fromisoformat(stamp).timestamp(),
        )
        # A later profile change never moves a historical meal to another day.
        await connection.execute(
            sa.insert(profile).values(id=1, timezone="Pacific/Auckland", created_at=WHEN)
        )
        totals = await daily_totals(connection, expected_date)
        assert totals.item_count == 1
        assert totals.recorded_timezones == (zone,)
        for adjacent in (expected_date - timedelta(days=1), expected_date + timedelta(days=1)):
            assert (await daily_totals(connection, adjacent)).item_count == 0


async def test_multiple_recorded_zones_and_chronological_order_are_stable(store, food):
    async with store.write() as connection:
        late = await create(connection, food, consumed_at=WHEN + 3600)
        early = await create(
            connection,
            food,
            action_key="earlier",
            source_message_id=11,
            timezone="Asia/Tokyo",
            consumed_at=datetime(2024, 1, 15, 12, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp(),
        )
        tied = await create(
            connection,
            food,
            action_key="tied",
            source_message_id=12,
            consumed_at=WHEN + 3600,
        )
        totals = await daily_totals(connection, DAY)
    assert [value.id for value in totals.meals] == [early.id, late.id, tied.id]
    assert totals.recorded_timezones == ("Asia/Tokyo", "UTC")


async def test_registered_extensions_follow_core_order_and_missing_stays_unknown(store):
    async with store.write() as connection:
        for code in ("z_synthetic", "a_synthetic"):
            await register_nutrient(
                connection,
                NutrientDefinition(
                    code=code, name=code, unit="mg", definition="Synthetic extension for tests"
                ),
            )
        extended = await publish_reviewed_food(
            connection,
            reviewed_food(
                basis_grams="100",
                nutrients=[{"code": "z_synthetic", "amount": "0.5", "unit": "mg"}],
            ),
        )
        await create(connection, extended, items=(measured(extended, "200"),))
        totals = await daily_totals(connection, DAY)
    assert tuple(value.code for value in totals.nutrients) == (
        *CORE_NUTRIENT_ORDER,
        "a_synthetic",
        "z_synthetic",
    )
    assert values(totals)["a_synthetic"].known_amount_scaled is None
    assert values(totals)["a_synthetic"].missing_items == 1
    assert values(totals)["z_synthetic"].known_amount_scaled == 1_000_000
    assert values(totals)["energy"].known_amount_scaled is None


async def test_sum_exceeding_sqlite_int64_remains_exact(store):
    async with store.write() as connection:
        huge = await publish_reviewed_food(
            connection,
            reviewed_food(
                basis_grams="0.001",
                nutrients=[{"code": "energy", "amount": "90000000", "unit": "kcal"}],
            ),
        )
        await create(connection, huge, items=(measured(huge, "100"), measured(huge, "100")))
        total = values(await daily_totals(connection, DAY))["energy"]
    assert total.known_amount_scaled == 18_000_000_000_000_000_000 > MAX_INTEGER
    assert total.known_items == 2


async def test_sum_uses_consumed_snapshot_rounding_without_rescaling(store):
    async with store.write() as connection:
        tiny = await publish_reviewed_food(
            connection,
            reviewed_food(
                basis_grams="100",
                nutrients=[{"code": "protein", "amount": "0.000003", "unit": "g"}],
            ),
        )
        await create(connection, tiny, items=(measured(tiny, "50"), measured(tiny, "50")))
        total = values(await daily_totals(connection, DAY))["protein"]
    assert total.known_amount_scaled == 4
    assert total.known_items == 2


async def test_source_and_quality_coverage_distinguish_manual_and_database_values(store, food):
    async with store.write() as connection:
        sourced = await publish_reviewed_food(
            connection,
            reviewed_food(
                name="Synthetic sourced food",
                basis_grams="100",
                nutrients=[
                    {"code": "energy", "amount": "100", "unit": "kcal"},
                    {"code": "vitamin_c", "amount": "0", "unit": "mg"},
                    {"code": "sodium", "amount": None, "unit": "mg"},
                ],
            ),
            provenance=SourceProvenance(
                external_id="12345",
                fetched_at=WHEN,
                adapter_version="synthetic-v1",
                data_type="Foundation",
            ),
        )
        await create(
            connection,
            food,
            items=(measured(food, "100"), measured(sourced, "100"), measured(sourced, "50")),
        )
        totals = await daily_totals(connection, DAY)
    assert totals.source_counts == (("manual_reviewed", 1), ("usda", 2))
    assert totals.quantity_counts == (("measured", 3),)
    assert values(totals)["energy"].quality_counts == (
        ("manual_reviewed", 1),
        ("source_reported", 2),
    )
    assert values(totals)["energy"].known_amount_scaled == 350_000_000
    assert values(totals)["vitamin_c"].quality_counts == (("source_reported", 2),)
    assert values(totals)["vitamin_c"].known_amount_scaled == 0
    assert values(totals)["sodium"].quality_counts == (("manual_reviewed", 1),)
    assert values(totals)["sodium"].missing_items == 2
