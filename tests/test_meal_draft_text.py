from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal, localcontext

import pytest

from nutrition_bot.domain.meal_draft_text import parse_draft_meal
from nutrition_bot.domain.meal_text import MEAL_TEXT_HELP, MealTextError, parse_meal

TODAY = date(2026, 9, 21)


def test_mixed_draft_keeps_exact_quantities_and_explicit_estimate_basis():
    meal = parse_draft_meal("/meal yesterday Lunch: 150g rice; about 0.200 KG chicken; #12", TODAY)
    assert meal.label == "Lunch"
    assert meal.local_date == date(2026, 9, 20)
    assert [(item.query, item.grams) for item in meal.items] == [
        ("rice", Decimal(150)),
        ("chicken", Decimal(200)),
        ("#12", None),
    ]
    assert meal.items[0].estimate_basis is None
    assert meal.items[1].estimate_basis == "User-described approximate amount"
    assert meal.items[1].original_quantity == "0.200"
    assert meal.items[1].original_unit == "KG"
    assert meal.items[2].original_quantity is None
    assert meal.items[2].original_unit is None


@pytest.mark.parametrize(
    "prefix", ["about ", "around ", "approximately ", "approx ", "roughly ", "estimated ", "~"]
)
@pytest.mark.parametrize("item", ["150g rice", "rice 150g"])
def test_only_explicit_prefixes_mark_quantities_as_estimates(prefix, item):
    draft = parse_draft_meal(f"{prefix}{item}", TODAY).items[0]
    assert draft.query == "rice"
    assert draft.grams == 150
    assert draft.estimate_basis == "User-described approximate amount"
    with pytest.raises(MealTextError):
        parse_meal(f"{prefix}{item}", TODAY)


@pytest.mark.parametrize(
    "text,queries",
    [
        ("I ate rice and chicken", ["rice", "chicken"]),
        ("Rice, white, cooked; Milk, 2% fat", ["Rice, white, cooked", "Milk, 2% fat"]),
        ("rice with oil; #12", ["rice with oil", "#12"]),
        ("I ate Հավի միս; Яблоко", ["Հավի միս", "Яблоко"]),
        ("/meal@SyntheticBot VitaminD3 powder", ["VitaminD3 powder"]),
    ],
)
def test_missing_quantities_preserve_the_entire_query_for_exact_resolution(text, queries):
    meal = parse_draft_meal(text, TODAY)
    assert [item.query for item in meal.items] == queries
    assert all(item.grams is None and item.estimate_basis is None for item in meal.items)


@pytest.mark.parametrize(
    "text,day,label",
    [
        ("I ate today Dinner: rice", TODAY, "Dinner"),
        ("2026-09-01 Snack: about 150g rice", date(2026, 9, 1), "Snack"),
        ("/meal yesterday rice", date(2026, 9, 20), "Meal"),
    ],
)
def test_explicit_dates_and_labels_use_the_measured_grammar(text, day, label):
    meal = parse_draft_meal(text, TODAY)
    assert meal.local_date == day
    assert meal.label == label


@pytest.mark.parametrize(
    "text",
    [
        "",
        "/meal",
        "I ate",
        "about rice",
        "~rice",
        "roughly #12",
        "about around 150g rice",
        "probably 150g rice",
        "maybe rice",
        "some rice",
        "handful rice",
        "3 eggs",
        "two slices bread",
        "2 rice",
        "150ml milk",
        "rice 200ml",
        "rice 150-200g",
        "150–200g rice",
        "rice 1/2kg",
        "1,5kg rice",
        "rice 150g-ish",
        "150ish g rice",
        "rice approximately 150g",
        "150g rice roughly",
        "approximately 0g rice",
        "rice ~150g",
        "≈150g rice",
        "about ≈150g rice",
        "~about 150g rice",
        "-150g rice",
        "+150g rice",
        "50.001kg rice",
        "0.1mg rice",
        "rice 150g; some oil",
        "150g rice plus oil",
        "rice or chicken",
        "rice then oil",
        "rice also oil",
        "rice + oil",
        "rice 2 eggs 150g",
        "150g rice, 200g chicken",
        "rice;",
        ";rice",
        "rice;;chicken",
        "rice\n\nchicken",
        "and rice",
        "rice and",
        "#12 cooked",
        "#01",
        "#0",
        "#9223372036854775808",
        "rice yesterday",
        "Lunch: yesterday rice",
        "tomorrow rice",
        "last night rice",
        "2026-09-22 rice",
        "2026-02-30 rice",
        "09/21/2026 rice",
        "next Monday rice",
        "/other rice",
        "/mealish rice",
        "rice!",
        "rice?",
        "rice\x00",
        "rice\u202e",
        "rice; about 150g chicken; 3 eggs",
        "about 150g rice with 200g oil",
    ],
)
def test_unsupported_input_fails_completely_without_echoing_raw_text(text):
    with pytest.raises(MealTextError) as error:
        parse_draft_meal(text, TODAY)
    assert str(error.value) == MEAL_TEXT_HELP


def test_bounded_input_precision_and_immutable_output():
    with localcontext() as context:
        context.prec = 2
        item = parse_draft_meal("about .000001kg rice", TODAY).items[0]
    assert item.grams == Decimal("0.001")
    with pytest.raises(FrozenInstanceError):
        item.grams = Decimal(500)
    assert len(parse_draft_meal(";".join(["rice"] * 10), TODAY).items) == 10
    assert parse_draft_meal("x" * 500, TODAY).items[0].query == "x" * 500
    for text in (";".join(["rice"] * 11), "x" * 501, "x" * 6001):
        with pytest.raises(MealTextError):
            parse_draft_meal(text, TODAY)


def test_calendar_boundary_errors_remain_controlled():
    assert parse_draft_meal("yesterday rice", date(2024, 3, 1)).local_date == date(2024, 2, 29)
    with pytest.raises(MealTextError):
        parse_draft_meal("yesterday rice", date.min)
