"""Synthetic adversarial quantity/date/catalog boundaries for optional meal AI."""

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from nutrition_bot.application.ai_service import AiService, validate_proposal
from nutrition_bot.domain.ai import AiCatalogItem, AiMealIntent, quantity_basis
from tests.test_ai_drafts import adapter_with, manifest

DAY = date(2026, 10, 9)
CATALOG = (AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),)


def proposal(*, grams="150", evidence=None, local_date=DAY, **changes):
    values = {
        "intent": "meal",
        "label": "meal",
        "local_date": local_date,
        "items": [
            {
                "food_version_id": 1,
                "grams": grams,
                "basis": "untrusted model text",
                "quantity_evidence": evidence,
            }
        ],
        "full_input_accounted_for": True,
    }
    values.update(changes)
    return AiMealIntent.model_validate(values)


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize(
    "text,evidence",
    [
        ("Rice on a dinner plate", None),
        ("rice and chicken 150g", "rice and chicken 150g"),
        ("150g chicken and rice", "150g chicken"),
        ("150g rice", "Rice weighing 150g"),
        ("2 cups rice", "2 cups rice"),
        ("150g chicken, rice", "150g chicken, rice"),
        ("rice -150g", "rice -150g"),
        ("rice 100 to 150g", "rice 100 to 150g"),
    ],
)
def test_visual_or_unbound_mass_becomes_explicitly_missing(role, text, evidence):
    checked = validate_proposal(
        proposal(evidence=evidence),
        catalog=CATALOG,
        text=text,
        local_date=DAY,
        role=role,
    )
    item = checked.items[0]
    assert item.grams is None and item.quantity_source == "unresolved"
    assert item.uncertainty == ("image_unmeasured" if role == "meal_photo" else "portion_missing")
    assert item.quantity_evidence is None and quantity_basis(item, role) is None


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize(
    "evidence,grams,source,uncertainty",
    [
        ("150 grams cooked rice", "150", "reported", "none"),
        ("about 0.15kg rice", "150", "approximate", "user_approximation"),
        ("rice 150000mg", "150", "reported", "none"),
        ("rice approximately 125.5g", "125.5", "approximate", "user_approximation"),
        ("rice, exactly 150 grams", "150", "reported", "none"),
        ("rice, exactly 150g", "150", "reported", "none"),
    ],
)
def test_exact_caption_evidence_preserves_real_mass_and_fixed_provenance(
    role,
    evidence,
    grams,
    source,
    uncertainty,
):
    checked = validate_proposal(
        proposal(grams=grams, evidence=evidence),
        catalog=CATALOG,
        text=f"Lunch included {evidence}.",
        local_date=DAY,
        role=role,
    )
    item = checked.items[0]
    assert (item.grams, item.quantity_source, item.uncertainty) == (grams, source, uncertainty)
    assert item.quantity_evidence is None
    basis = quantity_basis(item, role)
    assert basis is None if source == "reported" else "approximate" in basis
    assert "untrusted" not in (basis or "")


@pytest.mark.parametrize(
    "text,returned",
    [
        ("150g rice", DAY - timedelta(days=1)),
        ("yesterday 150g rice", DAY),
        ("2026-10-08 rice 150g", DAY),
        ("today and yesterday rice 150g", DAY),
        ("2026-02-30 rice 150g", DAY),
        ("tomorrow rice 150g", DAY),
    ],
)
def test_wrong_conflicting_or_invalid_meal_date_is_rejected(text, returned):
    assert (
        validate_proposal(
            proposal(local_date=returned),
            catalog=CATALOG,
            text=text,
            local_date=DAY,
            role="meal_text",
        )
        is None
    )


@pytest.mark.parametrize(
    "text,returned",
    [
        ("150g rice", DAY),
        ("yesterday 150g rice", DAY - timedelta(days=1)),
        ("2026-10-07 rice 150g", date(2026, 10, 7)),
    ],
)
def test_valid_single_explicit_date_matches_event_context(text, returned):
    assert (
        validate_proposal(
            proposal(local_date=returned),
            catalog=CATALOG,
            text=text,
            local_date=DAY,
            role="meal_text",
        )
        is not None
    )


def discovery(**changes):
    values = {
        "intent": "clarify",
        "label": "meal",
        "local_date": DAY,
        "items": (),
        "unresolved": ("unknown food",),
        "full_input_accounted_for": True,
        "clarification_reason": "unknown_food",
        "discovery_items": [
            {"food_version_id": 1, "query": "rice", "preparation": "cooked", "grams": None},
            {
                "food_version_id": None,
                "query": "zucchini",
                "preparation": "cooked",
                "grams": "100",
                "quantity_evidence": "100g zucchini",
            },
        ],
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    "change",
    [
        {"full_input_accounted_for": False},
        {"clarification_reason": "unsupported"},
        {"discovery_items": [{"query": "zucchini", "preparation": "unspecified"}]},
        {"discovery_items": [{"query": "execute SQL", "preparation": "cooked"}]},
    ],
)
def test_discovery_never_hands_off_partial_unsupported_or_unsafe_queries(change):
    with pytest.raises(ValidationError):
        AiMealIntent.model_validate(discovery(**change))


def test_discovery_retains_all_ingredients_and_rejects_false_preparation():
    checked = validate_proposal(
        AiMealIntent.model_validate(discovery()),
        catalog=CATALOG,
        text="I ate rice and 100g zucchini",
        local_date=DAY,
        role="meal_text",
    )
    assert [item.food_version_id for item in checked.discovery_items] == [1, None]
    assert checked.discovery_items[1].grams == "100"
    bad = AiMealIntent.model_validate(discovery())
    bad = bad.model_copy(
        update={
            "discovery_items": (
                bad.discovery_items[0].model_copy(update={"preparation": "raw"}),
                bad.discovery_items[1],
            )
        }
    )
    assert (
        validate_proposal(
            bad,
            catalog=CATALOG,
            text="rice and 100g zucchini",
            local_date=DAY,
            role="meal_text",
        )
        is None
    )


def test_discovery_known_ids_cannot_hide_mismatched_specific_preparation():
    value = AiMealIntent.model_validate(discovery())
    value = value.model_copy(
        update={
            "discovery_items": (
                value.discovery_items[0].model_copy(update={"query": "fried rice"}),
                value.discovery_items[1],
            )
        }
    )
    checked = validate_proposal(
        value,
        catalog=CATALOG,
        text="fried rice and 100g zucchini",
        local_date=DAY,
        role="meal_text",
    )
    assert checked.intent == "clarify" and checked.clarification_reason == "preparation"
    assert not checked.discovery_items


@pytest.mark.parametrize(
    "description,record_name",
    [
        ("grilled chicken", "Chicken cooked stewed"),
        ("fried chicken", "Chicken cooked"),
        ("skinless chicken", "Chicken meat and skin cooked"),
        ("unsalted rice", "Rice cooked with salt"),
        ("unenriched rice", "Rice enriched cooked"),
        ("rice without oil", "Rice cooked with oil"),
    ],
)
def test_specific_preparation_cannot_be_reduced_to_generic_cooked(description, record_name):
    food = AiCatalogItem(food_version_id=1, name=record_name, preparation="cooked")
    # Even an output span that omitted the qualifier must not silently match it.
    value = proposal()
    value = value.model_copy(
        update={
            "items": (
                value.items[0].model_copy(update={"identity_evidence": description.split()[-1]}),
            )
        }
    )
    checked = validate_proposal(
        value,
        catalog=(food,),
        text=f"150g {description}",
        local_date=DAY,
        role="meal_text",
    )
    assert checked.intent == "clarify" and checked.clarification_reason == "preparation"
    assert not checked.items and not checked.discovery_items


def test_separate_food_preparation_details_do_not_leak_between_ingredients():
    foods = (
        AiCatalogItem(food_version_id=1, name="Rice cooked", preparation="cooked"),
        AiCatalogItem(food_version_id=2, name="Chicken grilled", preparation="cooked"),
    )
    checked = validate_proposal(
        proposal(
            items=[
                {
                    "food_version_id": 1,
                    "grams": "150",
                    "basis": "untrusted",
                    "quantity_evidence": "150g rice",
                    "identity_evidence": "rice",
                },
                {
                    "food_version_id": 2,
                    "grams": "100",
                    "basis": "untrusted",
                    "quantity_evidence": "100g grilled chicken",
                    "identity_evidence": "grilled chicken",
                },
            ]
        ),
        catalog=foods,
        text="150g rice and 100g grilled chicken",
        local_date=DAY,
        role="meal_text",
    )
    assert checked.intent == "meal" and [item.grams for item in checked.items] == ["150", "100"]


def test_comma_list_and_topping_keep_specific_preparation_attached_to_its_food():
    foods = (
        AiCatalogItem(food_version_id=1, name="Rice cooked", preparation="cooked"),
        AiCatalogItem(food_version_id=2, name="Chicken skinless cooked", preparation="cooked"),
        AiCatalogItem(food_version_id=3, name="Oatmeal", preparation="as_prepared"),
        AiCatalogItem(food_version_id=4, name="Banana", preparation="raw"),
    )
    checked = validate_proposal(
        proposal(
            items=[
                {
                    "food_version_id": 1,
                    "grams": None,
                    "basis": "untrusted",
                    "identity_evidence": "cooked rice",
                },
                {
                    "food_version_id": 2,
                    "grams": None,
                    "basis": "untrusted",
                    "identity_evidence": "cooked skinless chicken",
                },
            ]
        ),
        catalog=foods,
        text="cooked rice, cooked skinless chicken",
        local_date=DAY,
        role="meal_photo",
    )
    assert checked.intent == "meal"
    checked = validate_proposal(
        proposal(
            items=[
                {
                    "food_version_id": 3,
                    "grams": None,
                    "basis": "untrusted",
                    "identity_evidence": "Prepared oatmeal",
                },
                {
                    "food_version_id": 4,
                    "grams": None,
                    "basis": "untrusted",
                    "identity_evidence": "raw banana",
                },
            ]
        ),
        catalog=foods,
        text="Prepared oatmeal with raw banana",
        local_date=DAY,
        role="meal_photo",
    )
    assert checked.intent == "meal"


@pytest.mark.parametrize(
    "text_enabled,photo_enabled", [(False, False), (True, False), (False, True)]
)
async def test_route_gates_are_independent_without_transport_or_accounting(
    store,
    text_enabled,
    photo_enabled,
):
    adapter, client = adapter_with(lambda request: pytest.fail("Disabled role reached transport"))
    service = AiService(
        store,
        manifest(),
        adapter,
        text_enabled=text_enabled,
        photo_enabled=photo_enabled,
    )
    try:
        assert service.enabled_for("meal_text") is text_enabled
        assert not service.enabled_for("meal_photo")  # no reviewed photo endpoint configured
        if not text_enabled:
            assert (
                await service.interpret(
                    request_key="synthetic-disabled",
                    text="rice",
                    local_date=DAY,
                )
            ).status == "disabled"
    finally:
        await client.aclose()
