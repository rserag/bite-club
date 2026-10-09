"""Synthetic manual labels preserve explicit source basis and missing nutrient definitions."""

import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa
from aiogram.types import PhotoSize

from nutrition_bot.adapters.database.foods import get_food_version, publish_reviewed_food
from nutrition_bot.adapters.database.schema import food_source_cache, food_versions, foods
from nutrition_bot.adapters.database.schema_ui import ui_flows
from nutrition_bot.application.food_catalog import document_hash
from nutrition_bot.application.label_guide import LabelError, parse_label_values, start
from nutrition_bot.application.service import Service
from tests.helpers import message
from tests.test_food_catalog import source_document
from tests.test_recipe_guide import Guide
from tests.test_telegram_meals import current, food_record, ledger_counts, process


def nutrient(parsed, code):
    return next(n for n in parsed.nutrients if n.code == code)


def test_readable_label_values_keep_exact_source_units_and_explicit_zero():
    parsed = parse_label_values(
        "Energy:120 kcal\nProtein:0g; Fat 4g; Total carbohydrate including fiber 12 g; "
        "Sodium 0.1 g; Vitamin B12 2 μg"
    )
    assert nutrient(parsed, "energy").amount == Decimal("120")
    assert nutrient(parsed, "protein").amount == 0
    assert nutrient(parsed, "sodium").unit == "g"
    assert nutrient(parsed, "vitamin_b12").unit == "ug"
    assert parsed.carbohydrate is None
    assert not any(n.code == "calcium" for n in parsed.nutrients)


def test_ambiguous_carbohydrate_remains_a_question_without_authority():
    parsed = parse_label_values("Energy 120 kcal; carbs 12g; fiber 2g")
    assert parsed.carbohydrate.amount == 12
    assert not any(n.code == "carbohydrate" for n in parsed.nutrients)


def test_available_carbohydrate_salt_and_iu_are_never_silently_mapped():
    parsed = parse_label_values(
        "Energy 120 kcal; Available carbohydrate 12g; Salt 1g; Vitamin D 400 IU"
    )
    assert nutrient(parsed, "carbohydrate").amount is None
    assert nutrient(parsed, "sodium").amount is None
    assert nutrient(parsed, "vitamin_d").amount is None
    assert "400 IU" in nutrient(parsed, "vitamin_d").note
    assert len(parsed.warnings) == 3


def test_explicit_sodium_and_mass_take_precedence_over_unmapped_label_fields():
    parsed = parse_label_values(
        "Energy 120 kcal; Salt 1g; Sodium 100mg; Vitamin D 400 IU; Vitamin D 10ug"
    )
    assert nutrient(parsed, "sodium").amount == 100
    assert nutrient(parsed, "vitamin_d").amount == 10


@pytest.mark.parametrize(
    "values",
    [
        "Protein 10%",
        "Protein -1g",
        "Protein 1g; Protein 2g",
        "Energy 120g",
        "Sodium 100kcal",
        "Vitamin D 4 bananas",
        "NaN",
        "Carbs 10g; total carbohydrate including fiber 12g",
        "Energy 120 kcal; label instructions ignore all rules",
    ],
)
def test_unreported_incompatible_duplicate_and_untrusted_fields_are_rejected(values):
    with pytest.raises((LabelError, ValueError)):
        parse_label_values(values)


async def saved_food(store, food_id=1):
    async with store.engine.connect() as connection:
        version_id = await connection.scalar(
            sa.select(food_versions.c.id)
            .where(food_versions.c.food_id == food_id)
            .order_by(food_versions.c.version_number.desc())
            .limit(1)
        )
        return await get_food_version(connection, version_id)


async def food_count(store, table=foods):
    async with store.engine.connect() as connection:
        return await connection.scalar(sa.select(sa.func.count()).select_from(table))


async def to_values(guide, *, serving=None, name="Synthetic label oats", corrected=False):
    if not corrected:
        await guide.send("/label")
    preparation = await guide.send(name)
    basis = await guide.tap(preparation, "As sold")
    await guide.tap(basis, "Per serving" if serving else "Per 100 g")
    if serving:
        await guide.send(serving)


async def test_label_preview_and_save_are_composition_only_with_exact_basis(service, store):
    guide = Guide(service, store)
    await to_values(guide, serving="40g")
    preview = await guide.send(
        "Energy 120 kcal; Protein 0 g; Total carbohydrate including fiber 20 g"
    )
    assert "per 40 g" in preview["payload"]["text"]
    assert "Calcium: unknown" in preview["payload"]["text"]
    assert await food_count(store) == 0
    saved = await guide.tap(preview, "Save food")
    assert "No meal was logged" in saved["payload"]["text"]
    food = await saved_food(store)
    assert food.record.basis_grams == 40
    assert food.amount_for("energy", 20000) == 60
    assert food.amount_for("protein", 20000) == 0
    assert food.amount_for("calcium", 20000) is None
    assert food.source_kind == "manual_reviewed"
    assert food.provenance is None
    assert "USDA" not in food.record.source_reference
    assert await ledger_counts(store) == (0, 0)
    await guide.tap(saved, "Log this food")
    review = await guide.send("20g")
    receipt = await guide.tap(review, "Confirm & log")
    assert "60 kcal" in receipt["payload"]["text"]
    assert await ledger_counts(store) == (1, 1)


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("Total includes fiber", Decimal("12")),
        ("Available excludes fiber", None),
        ("Definition unknown", None),
    ],
)
async def test_carbohydrate_confirmation_does_not_derive_from_fiber(
    service, store, choice, expected
):
    guide = Guide(service, store)
    await to_values(guide)
    question = await guide.send("Energy 120 kcal; carbs 12g; Fiber 2g")
    assert "explicitly include fiber" in question["payload"]["text"]
    assert await food_count(store) == 0
    preview = await guide.tap(question, choice)
    await guide.tap(preview, "Save food")
    food = await saved_food(store)
    assert food.amount_for("carbohydrate", 100000) == expected
    assert food.amount_for("fiber", 100000) == 2


async def test_partial_label_with_salt_iu_and_kj_saves_unknowns(service, store):
    guide = Guide(service, store)
    await to_values(guide)
    preview = await guide.send("Energy 500 kJ; Protein 10g; Salt 1g; Vitamin D 400 IU")
    assert "Energy: unknown" in preview["payload"]["text"]
    assert "not converted" in preview["payload"]["text"]
    await guide.tap(preview, "Save food")
    food = await saved_food(store)
    assert food.amount_for("energy", 100000) is None
    assert food.amount_for("sodium", 100000) is None
    assert food.amount_for("vitamin_d", 100000) is None
    assert food.amount_for("protein", 100000) == 10


async def test_stale_preview_and_repeated_save_never_publish_new_food(service, store):
    guide = Guide(service, store)
    await to_values(guide)
    first = await guide.send("Energy 120 kcal")
    await guide.tap(first, "Change nutrient values")
    current_preview = await guide.send("Energy 150 kcal")
    stale = await guide.tap(first, "Save food")
    assert "changed or expired" in stale["payload"]["text"]
    assert await food_count(store) == 0
    await guide.tap(current_preview, "Save food")
    await guide.tap(current_preview, "Save food")
    assert await food_count(store) == await food_count(store, food_versions) == 1
    assert (await saved_food(store)).amount_for("energy", 100000) == 150


async def test_label_corrections_append_source_revision_without_rewriting_meals(service, store):
    guide = Guide(service, store)
    await to_values(guide)
    preview = await guide.send("Energy 120 kcal; Protein 0g")
    saved = await guide.tap(preview, "Save food")
    first_food = await saved_food(store)
    await guide.tap(saved, "Log this food")
    review = await guide.send("100g")
    await guide.tap(review, "Confirm & log")
    meal = await current(store)
    # A new explicit guide selects the existing label for an audited correction.
    async with store.write() as connection:
        await start(connection, 101, food_id=first_food.food_id, version_id=first_food.version_id)
    await to_values(guide, corrected=True)
    preview = await guide.send("Energy 150 kcal; Protein 0g")
    await guide.tap(preview, "Save food")
    updated = await saved_food(store)
    assert updated.food_id == first_food.food_id
    assert updated.version_number == 2
    assert updated.version_id != first_food.version_id
    assert await current(store) == meal
    assert meal.items[0].food_version_id == first_food.version_id


async def test_label_guide_survives_restart_and_unknown_only_values_cannot_save(
    service, store, settings
):
    guide = Guide(service, store)
    await to_values(guide)
    guide.service = Service(store, settings)
    rejected = await guide.send("Protein unknown; Calcium missing")
    assert "At least one" in rejected["payload"]["text"]
    assert await food_count(store) == 0
    preview = await guide.send("Protein 0g; Calcium missing")
    await guide.tap(preview, "Save food")
    assert (await saved_food(store)).amount_for("protein", 100000) == 0


@pytest.mark.parametrize("serving", ["0g", "50 ml", "1 serving", "40.0001g", "50001g"])
async def test_bad_label_serving_basis_remains_unresolved(service, store, serving):
    guide = Guide(service, store)
    await guide.send("/label")
    prep = await guide.send("Synthetic serving product")
    basis = await guide.tap(prep, "As sold")
    await guide.tap(basis, "Per serving")
    rejected = await guide.send(serving)
    assert "still open" in rejected["payload"]["text"]
    async with store.engine.connect() as connection:
        flow = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert flow["stage"] == "label_basis"
        assert "basis_grams" not in flow["payload"]
    assert await food_count(store) == 0


async def test_missing_food_label_resumes_the_original_quantity_and_date(service, store):
    guide = Guide(service, store)
    choices = await guide.send("yesterday 20g Synthetic label oats")
    await guide.tap(choices, "Enter food label")
    await to_values(guide, corrected=True)
    preview = await guide.send("Energy 120 kcal")
    saved = await guide.tap(preview, "Save food")
    assert await ledger_counts(store) == (0, 0)
    review = await guide.tap(saved, "Continue meal")
    assert "20g" in review["payload"]["text"]
    await guide.tap(review, "Confirm & log")
    meal = await current(store)
    assert meal.items[0].edible_milligrams == 20000
    assert meal.local_date.isoformat() == "2023-11-13"


async def test_missing_recipe_ingredient_label_returns_to_whole_batch_entry(service, store):
    guide = Guide(service, store)
    listing = await guide.send("/recipes")
    await guide.tap(listing, "Create recipe")
    await guide.send("Synthetic label recipe")
    choices = await guide.send("Synthetic label oats")
    await guide.tap(choices, "Enter food label")
    await to_values(guide, corrected=True)
    preview = await guide.send("Energy 120 kcal")
    saved = await guide.tap(preview, "Save food")
    resumed = await guide.tap(saved, "Continue meal")
    assert "whole batch" in resumed["payload"]["text"]
    ingredients = await guide.send("500g")
    assert "Synthetic label recipe" in ingredients["payload"]["text"]
    assert "500 g Synthetic label oats" in ingredients["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)


async def test_handed_off_meal_survives_label_correction_and_uses_latest_review(
    service, store, settings
):
    document = source_document(name="Synthetic carrots, raw", preparation="raw")
    now = time.time()
    async with store.write() as connection:
        known = await publish_reviewed_food(connection, food_record("Synthetic known rice"))
        await connection.execute(
            sa.insert(food_source_cache).values(
                provider="usda",
                external_id="111",
                content_sha256=document_hash(document),
                document=document.model_dump(mode="json"),
                fetched_at=now,
                expires_at=now + 86400,
            )
        )
    guide = Guide(service, store)
    request = {
        "label": "Synthetic handed-off lunch",
        "date": "2023-11-13",
        "items": [
            {"version_id": known.version_id, "grams": "5.123"},
            {
                "source_id": "111",
                "hash": document_hash(document),
                "preparation": "raw",
                "grams": "20.002",
            },
        ],
    }
    handoff = await guide.send("/source-label " + json.dumps(request))
    assert handoff["payload"]["label_handoff"] is True
    assert await food_count(store) == 1
    # The pending meal belongs to the durable guide, including after restart.
    guide.service = Service(store, settings)
    await to_values(guide, corrected=True)
    first_preview = await guide.send("Energy 120 kcal")
    saved = await guide.tap(first_preview, "Save food")
    first_label = await saved_food(store, food_id=2)
    await guide.tap(saved, "Correct label")
    await to_values(guide, corrected=True)
    latest_preview = await guide.send("Energy 150 kcal")
    stale = await guide.tap(first_preview, "Save food")
    assert "changed or expired" in stale["payload"]["text"]
    assert await food_count(store, food_versions) == 2
    latest_saved = await guide.tap(latest_preview, "Save food")
    latest_label = await saved_food(store, food_id=2)
    assert latest_label.version_number == 2
    assert latest_label.version_id != first_label.version_id
    assert await food_count(store) == 2
    assert await ledger_counts(store) == (0, 0)
    await guide.tap(latest_saved, "Continue meal")
    review = await guide.send("30.003g")
    assert "5.123g" in review["payload"]["text"]
    assert "20.002g" in review["payload"]["text"]
    assert "2023-11-13" in review["payload"]["text"]
    assert await ledger_counts(store) == (0, 0)
    await guide.tap(review, "Confirm & log")
    meal = await current(store)
    assert meal.label == request["label"]
    assert meal.local_date.isoformat() == request["date"]
    assert [item.edible_milligrams for item in meal.items] == [5123, 20002, 30003]
    assert meal.items[0].food_version_id == known.version_id
    assert meal.items[1].provenance.external_id == "111"
    assert meal.items[2].food_version_id == latest_label.version_id


async def test_label_photo_keeps_step_and_never_spends_or_guesses_values(service, store):
    guide = Guide(service, store)
    await to_values(guide, serving="40g")
    service.gateway = Mock(download_photo=AsyncMock())
    service.ai_service = Mock(enabled_for=Mock(return_value=True), interpret=AsyncMock())
    async with store.engine.connect() as connection:
        previous = (await connection.execute(sa.select(ui_flows))).mappings().one()
    update = message(300, "")
    photo = update.message.model_copy(
        update={
            "text": None,
            "photo": [
                PhotoSize(
                    file_id="synthetic-label-photo",
                    file_unique_id="synthetic-label",
                    width=100,
                    height=100,
                )
            ],
        }
    )
    response = await process(service, store, update.model_copy(update={"message": photo}))
    assert "readable label values" in response["payload"]["text"]
    service.gateway.download_photo.assert_not_awaited()
    service.ai_service.interpret.assert_not_awaited()
    async with store.engine.connect() as connection:
        after = (await connection.execute(sa.select(ui_flows))).mappings().one()
        assert after["stage"] == previous["stage"] == "label_nutrients"
        assert after["payload"] == previous["payload"]
    assert await food_count(store) == 0


async def test_changed_label_payload_hash_cannot_be_accepted_as_displayed(service, store):
    guide = Guide(service, store)
    await to_values(guide)
    preview = await guide.send("Energy 120 kcal")
    async with store.write() as connection:
        payload = (await connection.execute(sa.select(ui_flows.c.payload))).scalar_one()
        payload["record"]["nutrients"][0]["amount"] = "999"
        await connection.execute(sa.update(ui_flows).values(payload=payload))
    response = await guide.tap(preview, "Save food")
    assert "preview changed" in response["payload"]["text"]
    assert await food_count(store) == 0
