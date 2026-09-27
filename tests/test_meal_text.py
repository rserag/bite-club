from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal, localcontext

import pytest

from nutrition_bot.domain.meal_text import MEAL_TEXT_HELP, MealTextError, parse_meal

TODAY = date(2026, 9, 21)


def test_measured_meal_retains_every_food_and_original_measure():
    meal = parse_meal("/meal Lunch: 150g rice; 0.200 KG chicken", TODAY)
    assert meal.label == "Lunch"
    assert meal.local_date == TODAY
    assert [(item.query, item.grams) for item in meal.items] == [
        ("rice", Decimal(150)),
        ("chicken", Decimal(200)),
    ]
    assert meal.items[1].original_quantity == "0.200"
    assert meal.items[1].original_unit == "KG"


@pytest.mark.parametrize(
    "text,queries",
    [
        ("I ate 150g rice and 200g chicken", ["rice", "chicken"]),
        ("rice 150 g and chicken 200 g", ["rice", "chicken"]),
        ("BREAKFAST: 2.125g Paprika\nCottage cheese 150g", ["Paprika", "Cottage cheese"]),
        ("/meal@SyntheticBot 150g #12", ["#12"]),
        ("150g Rice, white, cooked; 250g Milk, 2% fat", ["Rice, white, cooked", "Milk, 2% fat"]),
        ("150g Հավի միս; 250g Яблоко", ["Հավի միս", "Яблоко"]),
        ("150g Rice with oil", ["Rice with oil"]),
        ("0.100kg VitaminD3 powder", ["VitaminD3 powder"]),
    ],
)
def test_names_remain_complete_for_exact_catalog_matching(text, queries):
    assert [item.query for item in parse_meal(text, TODAY).items] == queries


@pytest.mark.parametrize(
    "text,day,label",
    [
        ("/meal yesterday Dinner: 150g rice", date(2026, 9, 20), "Dinner"),
        ("/meal today 150g rice", TODAY, "Meal"),
        ("2026-09-01 Snack: 150g rice", date(2026, 9, 1), "Snack"),
        ("I ate yesterday 150g rice", date(2026, 9, 20), "Meal"),
        ("/meal 2026-09-21 150g rice", TODAY, "Meal"),
    ],
)
def test_only_explicit_dates_change_the_local_day(text, day, label):
    meal = parse_meal(text, TODAY)
    assert meal.local_date == day
    assert meal.label == label


def test_yesterday_handles_calendar_boundaries():
    assert parse_meal("yesterday 100g rice", date(2024, 3, 1)).local_date == date(2024, 2, 29)
    with pytest.raises(MealTextError):
        parse_meal("yesterday 100g rice", date.min)


@pytest.mark.parametrize(
    "measure,grams",
    [
        ("1mg", "0.001"),
        (".001g", "0.001"),
        (".000001kg", "0.001"),
        ("150.000000000000000000g", "150"),
        ("50kg", "50000"),
    ],
)
def test_mass_conversion_is_exact_to_milligrams(measure, grams):
    with localcontext() as context:
        context.prec = 2
        assert parse_meal(f"{measure} food", TODAY).items[0].grams == Decimal(grams)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I ate",
        "/meal",
        "food",
        "150g",
        "g rice",
        "0g rice",
        "-150g rice",
        "rice -150g",
        "rice - 150g",
        "+150g rice",
        "rice +150g",
        "150-200g rice",
        "150–200g rice",
        "150g rice and -200g chicken",
        "1/2kg rice",
        "rice 1 / 2kg",
        "1,5kg rice",
        "1e3g rice",
        "150.5.5g rice",
        "0.1mg rice",
        "0.0001g rice",
        "50.001kg rice",
        "50000001mg rice",
        "NaNg rice",
        "Infinityg rice",
        "about 150g rice",
        "~150g rice",
        "≈150g rice",
        "rice approximately 150g",
        "150g rice approx",
        "150g-ish rice",
        "150ish g rice",
        "150g rice probably",
        "3 eggs",
        "2 slices bread",
        "150ml milk",
        "250 g rice and a banana",
        "150g rice; oil",
        "150g rice and some oil",
        "150g rice + oil",
        "150g rice plus oil",
        "150g rice;",
        ";150g rice",
        "150g rice;;200g chicken",
        "150g rice\n\n200g chicken",
        "150g rice and",
        "and 150g rice",
        "150g rice 200g chicken",
        "150g rice with 200g oil",
        "rice 2 eggs 150g",
        "150g rice 200ml oil",
        "150g rice 2",
        "150g rice then oil",
        "150g rice, 200g chicken",
        "150g rice; 200g chicken; 3 eggs",
        "/meal tomorrow 150g rice",
        "yesterdayish 150g rice",
        "150g rice yesterday",
        "2026-09-22 150g rice",
        "2026-02-30 150g rice",
        "09/21/2026 150g rice",
        "next Monday 150g rice",
        "Lunch: yesterday 150g rice",
        "last night 150g rice",
        "/other 150g rice",
        "/mealish 150g rice",
        "150g rice!",
        "150g rice?",
        "150g #0",
        "150g #-1",
        "150g #01",
        "150g #12 cooked",
        "150g #9223372036854775808",
        "150g rice\x00",
        "150g rice\u202e",
        "150g #12 and 200g #13 extra",
    ],
)
def test_unsupported_or_partial_meals_fail_as_a_whole_with_fixed_help(text):
    with pytest.raises(MealTextError) as error:
        parse_meal(text, TODAY)
    assert str(error.value) == MEAL_TEXT_HELP


def test_input_and_item_limits():
    assert len(parse_meal(";".join(["150g rice"] * 10), TODAY).items) == 10
    assert parse_meal("150g " + "x" * 500, TODAY).items[0].query == "x" * 500
    for text in (";".join(["150g rice"] * 11), "150g " + "x" * 501, "1" * 6000 + "g rice"):
        with pytest.raises(MealTextError):
            parse_meal(text, TODAY)


def test_parsed_meal_is_immutable():
    meal = parse_meal("150g rice", TODAY)
    assert isinstance(meal.items, tuple)
    with pytest.raises(FrozenInstanceError):
        meal.items[0].grams = Decimal(500)
    with pytest.raises(FrozenInstanceError):
        meal.local_date = date(2026, 9, 20)
