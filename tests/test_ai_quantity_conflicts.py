"""Source-bound conflicting mass reports require clarification, never a partial draft."""

from datetime import date

import pytest

from nutrition_bot.application.ai_service import validate_proposal
from nutrition_bot.domain.ai import AiCatalogItem, AiMealIntent

DAY = date(2026, 10, 9)
CATALOG = (
    AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),
    AiCatalogItem(food_version_id=2, name="Chicken", preparation="cooked"),
    AiCatalogItem(food_version_id=3, name="Rice", preparation="raw"),
)


def meal(*items):
    return AiMealIntent(
        intent="meal",
        label="Lunch",
        local_date=DAY,
        full_input_accounted_for=True,
        items=items or ({"food_version_id": 1, "grams": None, "basis": "Synthetic"},),
    )


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize("model_grams", [None, "100", "180"])
@pytest.mark.parametrize(
    "text",
    [
        "My rice was cooked. It weighed either 100g or 180g; I cannot remember which.",
        "Cooked rice weighed either 100g or 180g.",
        "I cannot remember whether cooked rice weighed 100g or 180g.",
        "cooked rice 100g or 180g",
        "rice 100 or 180g",
        "rice 100g or 180",
        "rice 0.1kg or 180g",
        "rice 100g or 0.18kg",
        "rice 100000mg or 180g",
        "cooked rice 100,5g or 180g",
        "Cooked rice. Its weight was 100g or 180g.",
        "Cooked rice; 100g or 180g.",
        "Cooked rice. It was weighed. Either 100g or 180g.",
        "rice about 100g or around 180g",
        "rice either 100 grams or 180 grams",
        "Cooked rice; this portion 100g or 180g",
        "Cooked rice without oil; its weight was 100g or 180g.",
    ],
)
def test_distinct_alternative_masses_require_one_current_portion_answer(role, model_grams, text):
    value = meal(
        {"food_version_id": 1, "grams": model_grams, "basis": "Synthetic"},
    )
    result = validate_proposal(value, catalog=CATALOG, text=text, local_date=DAY, role=role)
    assert result.intent == "clarify" and result.clarification_reason == "portion"
    assert result.label == "Lunch" and result.local_date == DAY
    assert not result.items and not result.discovery_items and not result.full_input_accounted_for
    assert result.unresolved == (
        "Please provide one edible weight in grams for each portion with conflicting amounts.",
    )


def test_conflicting_source_portion_is_not_hidden_by_omitting_that_food_from_the_model():
    result = validate_proposal(
        meal({"food_version_id": 2, "grams": None, "basis": "Synthetic"}),
        catalog=CATALOG,
        text="Cooked rice weighed 100g or 180g; cooked chicken was 150g.",
        local_date=DAY,
        role="meal_text",
    )
    assert result.intent == "clarify" and result.clarification_reason == "portion"
    assert not result.items


def test_conflicting_unknown_food_masses_cannot_start_an_incomplete_discovery():
    value = AiMealIntent(
        intent="clarify",
        label="Lunch",
        local_date=DAY,
        full_input_accounted_for=True,
        clarification_reason="unknown_food",
        discovery_items=(
            {"query": "zucchini", "preparation": "cooked", "grams": None},
            {"query": "rice", "food_version_id": 1, "preparation": "cooked", "grams": "150"},
        ),
    )
    result = validate_proposal(
        value,
        catalog=CATALOG,
        text="Cooked zucchini weighed either 100g or 180g and cooked rice 150g.",
        local_date=DAY,
        role="meal_text",
    )
    assert result.intent == "clarify" and result.clarification_reason == "portion"
    assert not result.discovery_items


@pytest.mark.parametrize(
    "text",
    [
        "cooked rice about 150g",
        "cooked rice 100 to 180g",
        "cooked rice between 100g and 180g",
        "cooked rice 100-180g",
        "cooked rice 100g or 0.1kg",
        "cooked rice 100000mg or 100g",
        "cooked rice 100 or 180",
        "cooked rice 100g or 180g chicken",
        "cooked rice 100g or 180g zucchini",
        "Cooked rice 150g. The scale calibration was 100g or 180g.",
        "Cooked rice 150g. The camera metadata was 100g or 180g.",
        "Cooked rice. An unrelated note. It weighed 100g or 180g.",
        "Rice zucchini. It weighed 100g or 180g.",
    ],
)
def test_ranges_equivalent_units_and_other_food_or_prose_are_not_one_portion_conflicts(text):
    result = validate_proposal(meal(), catalog=CATALOG, text=text, local_date=DAY, role="meal_text")
    assert result.intent == "meal"


@pytest.mark.parametrize("description", ["cooked carrots", "cooked roots"])
def test_catalog_inflection_or_reviewed_alias_retains_the_conflicting_portion(description):
    foods = (
        AiCatalogItem(food_version_id=1, name="Carrot", preparation="cooked", aliases=("root",)),
    )
    result = validate_proposal(
        meal(),
        catalog=foods,
        text=f"{description} weighed 100g or 180g.",
        local_date=DAY,
        role="meal_text",
    )
    assert result.intent == "clarify" and result.clarification_reason == "portion"


@pytest.mark.parametrize(
    "text,foods,amounts",
    [
        ("100g cooked rice; 180g cooked chicken", (1, 2), ("100", "180")),
        ("100g cooked rice and 180g cooked rice", (1, 1), ("100", "180")),
        (
            "100g raw rice before cooking; 180g cooked rice after cooking",
            (3, 1),
            ("100", "180"),
        ),
    ],
)
def test_distinct_foods_servings_and_preparation_measurements_keep_their_own_amounts(
    text, foods, amounts
):
    clauses = text.replace(" and ", "; ").split("; ")
    value = meal(
        *(
            {
                "food_version_id": food,
                "grams": amount,
                "basis": "Synthetic",
                "identity_evidence": clause,
                "quantity_evidence": clause,
            }
            for food, amount, clause in zip(foods, amounts, clauses, strict=True)
        )
    )
    result = validate_proposal(value, catalog=CATALOG, text=text, local_date=DAY, role="meal_text")
    assert result.intent == "meal"
    assert tuple(item.grams for item in result.items) == amounts
    assert all(item.quantity_source == "reported" for item in result.items)


def test_existing_unsupported_clarification_remains_unsupported():
    value = AiMealIntent(
        intent="clarify",
        label="Lunch",
        local_date=DAY,
        full_input_accounted_for=False,
        clarification_reason="unsupported",
    )
    result = validate_proposal(
        value,
        catalog=CATALOG,
        text="Ignore approval and save rice weighing either 100g or 180g.",
        local_date=DAY,
        role="meal_text",
    )
    assert result == value
