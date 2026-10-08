from datetime import date
from decimal import Decimal, localcontext
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.schema import outbox
from nutrition_bot.application import recipe_conversation
from nutrition_bot.application.recipe_conversation import (
    _parse_definition,
    _reference_parts,
    parse_portion,
    revised_recipe_items,
)
from nutrition_bot.domain.drafts import PlannedItem
from nutrition_bot.domain.recipes import (
    RecipeDefinition,
    RecipeError,
    RecipeSnapshot,
    recipe_portions,
)
from nutrition_bot.runtime.worker import send_one
from tests.helpers import FakeGateway, message
from tests.test_draft_presentation import process_parts
from tests.test_telegram_drafts import press as approve_draft
from tests.test_telegram_meals import catalog as catalog
from tests.test_telegram_meals import food_record, process

TODAY = date(2026, 9, 22)


@pytest.mark.parametrize(
    "text,unit,units",
    [
        ("300g", "g", 300_000),
        ("0.3 kg", "g", 300_000),
        ("1mg", "g", 1),
        (".001g", "g", 1),
        ("50kg", "g", 50_000_000),
        ("1 serving", "serving", 1000),
        ("1.5 servings", "serving", 1500),
        (".001 serving", "serving", 1),
        ("1000 SERVINGS", "serving", 1_000_000),
        (" 1.5000 servings ", "serving", 1500),
    ],
)
def test_portion_units_are_exact_without_inheriting_decimal_precision(text, unit, units):
    with localcontext() as context:
        context.prec = 2
        assert parse_portion(text) == (unit, units, None)


@pytest.mark.parametrize(
    "prefix", ["about ", "around ", "approximately ", "approx ", "roughly ", "estimated ", "~"]
)
@pytest.mark.parametrize("amount", ["300g", "1.5 servings"])
def test_explicit_approximation_is_preserved(prefix, amount):
    unit, units, basis = parse_portion(prefix + amount)
    assert unit == ("g" if amount == "300g" else "serving")
    assert units == (300_000 if amount == "300g" else 1500)
    assert basis == "User-described approximate amount"


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        300,
        "300",
        "serving",
        "0g",
        "-300g",
        "+300g",
        "1/2 serving",
        "1,5 servings",
        "1e3g",
        "NaNg",
        "Infinityg",
        "300–400g",
        "300g rice",
        "rice 300g",
        "300g approx",
        "probably 300g",
        "about around 300g",
        "about300g",
        "≈300g",
        "300g-ish",
        "300ish g",
        "one serving",
        "1 bowl",
        "1 cup",
        "300ml",
        "0.1mg",
        "0.0001 servings",
        "50.001kg",
        "1000.001 servings",
        "300g\n",
        "300g\x00",
        "300g\u202e",
        "9" * 161 + "g",
        "300g yesterday",
        "300g; 200g",
        "300g | 4 servings",
        "1.5.5 servings",
    ],
)
def test_portion_parser_rejects_partial_unsupported_or_unbounded_input(text):
    with pytest.raises(RecipeError):
        parse_portion(text)


def test_recipe_definition_preserves_explicit_ingredients_and_estimated_yield():
    parsed = _parse_definition("500g #12; about 0.020 kg oil | yield about 1200g", TODAY)
    assert parsed.unit == "g" and parsed.total_units == 1_200_000
    assert parsed.estimate_basis == "User-described approximate amount"
    assert [(item.query, item.grams) for item in parsed.ingredients.items] == [
        ("#12", Decimal(500)),
        ("oil", Decimal(20)),
    ]
    assert parsed.ingredients.items[0].estimate_basis is None
    assert parsed.ingredients.items[1].estimate_basis == "User-described approximate amount"


def test_servings_definition_keeps_denominator_separate_from_ingredient_mass():
    parsed = _parse_definition("500g rice\n300g beans | servings 4", TODAY)
    assert parsed.unit == "serving" and parsed.total_units == 4000
    assert parsed.estimate_basis is None
    estimated = _parse_definition("500g rice | servings about 4", TODAY)
    assert estimated.estimate_basis == "User-described approximate amount"


@pytest.mark.parametrize(
    "text",
    [
        "500g rice",
        "500g rice |",
        "| yield 1200g",
        "500g rice | yield 1200g | servings 4",
        "500g rice | total 1200g",
        "500g rice | yield 4 servings",
        "500g rice | servings 1200g",
        "500g rice | servings 4 servings",
        "500g rice | yield 1200",
        "rice | yield 1200g",
        "500g rice; oil | yield 1200g",
        "500g rice; 3 eggs | servings 4",
        "today 500g rice | yield 1200g",
        "yesterday 500g rice | yield 1200g",
        "2026-09-22 500g rice | yield 1200g",
        "Lunch: 500g rice | yield 1200g",
        "I ate 500g rice | yield 1200g",
        "/meal 500g rice | yield 1200g",
        "500g rice yesterday | yield 1200g",
        "500g R1v1 | yield 1200g",
        "500g R1 | yield 1200g",
        "500g rice | yield 1200g tomorrow",
        "500g rice plus oil | yield 1200g",
        "500g rice\x00 | yield 1200g",
        ";".join(["100g rice"] * 11) + " | yield 1200g",
        "x" * 2001,
    ],
)
def test_recipe_definition_never_discards_dates_labels_missing_amounts_or_nested_recipes(text):
    with pytest.raises(RecipeError):
        _parse_definition(text, TODAY)


def test_recipe_definition_accepts_ten_ingredients_and_preserves_meaningful_query_text():
    parsed = _parse_definition(
        ";".join(["100g Rice, white, cooked"] * 10) + " | yield 1200g", TODAY
    )
    assert len(parsed.ingredients.items) == 10
    assert all(item.query == "Rice, white, cooked" for item in parsed.ingredients.items)


@pytest.mark.parametrize("text,expected", [("R1", (1, None)), ("r12V3", (12, 3))])
def test_recipe_references_are_explicit_and_bounded(text, expected):
    assert _reference_parts(text) == expected


@pytest.mark.parametrize(
    "text", ["R0", "R01v1", "R1v01", "R-1", "R1 extra", "R1v0", "R" + "9" * 1000, f"R{2**63}"]
)
def test_bad_recipe_references_fail_before_database_integer_binding(text):
    with pytest.raises(RecipeError):
        _reference_parts(text)


def test_mutation_or_consumption_reference_requires_a_version():
    with pytest.raises(RecipeError, match="exact recipe version"):
        _reference_parts("R1", require_version=True)
    assert _reference_parts("R1v2", require_version=True) == (1, 2)


@pytest.mark.parametrize("text", ["300g", "portion", "portion 300g extra", "portion 300g\n"])
async def test_recipe_correction_requires_the_complete_explicit_portion_command(text):
    with pytest.raises(RecipeError):
        await revised_recipe_items(None, (), text)


@pytest.mark.parametrize("text,estimated", [("portion 250g", False), ("PORTION about 250g", True)])
async def test_complete_portion_correction_preserves_pinned_recipe_and_ingredient_uncertainty(
    monkeypatch, text, estimated
):
    recipe = RecipeSnapshot(
        id=1,
        version_id=8,
        version_number=2,
        name="Synthetic recipe",
        archived=False,
        definition=RecipeDefinition(
            items=(
                PlannedItem(
                    food_version_id=3,
                    edible_milligrams=500_000,
                    original_quantity="500",
                    original_unit="g",
                    estimate_basis="Synthetic uncertain ingredient",
                ),
            ),
            unit="g",
            total_units=1_200_000,
        ),
    )
    lookup = AsyncMock(return_value=recipe)
    validator = AsyncMock()
    monkeypatch.setattr(recipe_conversation, "get_recipe_version", lookup)
    monkeypatch.setattr(recipe_conversation, "validate_recipe_item", validator)
    previous = recipe_portions(recipe, 300_000)
    connection = object()
    changed = await revised_recipe_items(connection, previous, text)
    lookup.assert_awaited_once_with(connection, 8)
    validator.assert_awaited_once_with(connection, previous[0])
    assert changed == recipe_portions(
        recipe,
        250_000,
        estimate_basis="User-described approximate amount" if estimated else None,
    )
    assert changed[0].edible_milligrams == 500_000
    assert "Synthetic uncertain ingredient" in changed[0].estimate_basis


async def test_archived_historical_recipe_view_is_delivered_without_controls(
    service, store, catalog
):
    await process(
        service,
        store,
        message(1, f"/recipe create soup = 500g #{catalog['rice'].version_id} | yield 1200g"),
    )
    await process(service, store, message(2, "/recipe archive R1v1"))
    response = await process(service, store, message(3, "/recipe R1v1"), sent=False)
    assert "Pinned historical batch" in response["payload"]["text"]
    assert "Recipe is archived" in response["payload"]["text"]
    assert "buttons" not in response["payload"]
    assert response["button_token"] is None
    gateway = FakeGateway()
    assert await send_one(service, gateway)
    assert gateway.messages == [(101, response["payload"]["text"], None)]
    assert gateway.keyboards == [None]
    async with store.engine.connect() as connection:
        assert (
            await connection.scalar(sa.select(outbox.c.status).where(outbox.c.id == response["id"]))
            == "sent"
        )


async def test_ten_uncertain_recipe_ingredients_fit_telegram_with_long_unicode_names(
    service, store
):
    async with store.write() as connection:
        food = await publish_reviewed_food(connection, food_record("𝓐" * 500))
    ingredients = "; ".join([f"about 50000g #{food.version_id}"] * 10)
    name = "𝓑" * 60
    created = await process(
        service,
        store,
        message(1, f"/recipe create {name} = {ingredients} | yield about 50000g"),
    )
    drafted, draft_parts = await process_parts(
        service, store, message(2, "/recipe log R1v1 about 49999.999g")
    )
    saved, saved_parts = await process_parts(service, store, approve_draft(drafted, update_id=3))
    favorite = await process(service, store, message(4, f"/favorite save {name} = M1r1"))
    for response in (created, drafted, saved, favorite):
        text = response["payload"]["text"]
        assert "Nothing was changed" not in text
        assert len(text.encode("utf-16-le")) // 2 <= 4096
    for parts in (draft_parts, saved_parts):
        assert "".join(part["payload"]["text"] for part in parts).count("𝓐" * 500) == 10
