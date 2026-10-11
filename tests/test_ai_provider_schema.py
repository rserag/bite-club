"""Synthetic provider wire shapes retain the durable meal safety contract."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter, parse_completed_stream
from nutrition_bot.adapters.ai.openrouter import SYSTEM_PROMPT, _strict_schema
from nutrition_bot.application.ai_service import validate_proposal
from nutrition_bot.domain.ai import AiCatalogItem, AiMealIntent, AiProviderEnvelope, AiUnavailable
from tests.test_ai_drafts import (
    TODAY,
    adapter_with,
    completion,
    proposed,
    provider_payload,
    reviewed_route,
)
from tests.test_chatgpt_plan_ai import policy, stream


def wire_item(**changes):
    item = {
        "food_version_id": 1,
        "grams": "150",
        "quantity_source": "reported",
        "uncertainty": "none",
        "quantity_evidence": "150g cooked rice",
        "identity_evidence": "cooked rice",
        "basis": "User reported an edible mass.",
    }
    item.update(changes)
    return item


def clarification(**changes):
    result = {
        "kind": "clarify",
        "label": "meal",
        "local_date": TODAY.isoformat(),
        "full_input_accounted_for": False,
        "clarification_reason": "food_identity",
        "unresolved": ["Please name the foods and preparation."],
    }
    result.update(changes)
    return {"result": result}


@pytest.mark.parametrize(
    "reason", ["preparation", "food_identity", "portion", "unsupported", "image", "incomplete"]
)
@pytest.mark.parametrize("complete", [True, False])
def test_bounded_clarification_is_valid_without_mixing_partial_meal_fields(reason, complete):
    intent = AiProviderEnvelope.model_validate(
        clarification(clarification_reason=reason, full_input_accounted_for=complete)
    ).to_intent()
    assert intent.intent == "clarify" and intent.clarification_reason == reason
    assert not intent.items and not intent.discovery_items
    assert intent.full_input_accounted_for is complete


def test_old_flat_clarification_conflict_is_rejected_but_explicit_branch_is_valid():
    with pytest.raises(ValidationError):
        AiMealIntent.model_validate(
            proposed(intent="clarify", unresolved=["Please confirm preparation."])
        )
    intent = AiProviderEnvelope.model_validate(
        clarification(clarification_reason="preparation")
    ).to_intent()
    assert intent.intent == "clarify" and not intent.items


@pytest.mark.parametrize(
    "extra", [{"items": [wire_item()]}, {"discovery_items": [{}]}, {"save": True}]
)
def test_clarification_branch_rejects_surplus_meal_discovery_or_action_fields(extra):
    with pytest.raises(ValidationError):
        AiProviderEnvelope.model_validate(clarification(**extra))


@pytest.mark.parametrize(
    "changes",
    [
        {"items": []},
        {"full_input_accounted_for": False},
        {"full_input_accounted_for": 1},
        {"unresolved": ["missing food"]},
        {"discovery_items": [{}]},
        {"items": [wire_item(quantity_source="unspecified", uncertainty="unspecified")]},
        {"items": [wire_item(quantity_source="unresolved")]},
    ],
)
def test_provider_meal_rejects_incomplete_or_contradictory_shapes(changes):
    payload = provider_payload(proposed(items=[wire_item()]))
    payload["result"].update(changes)
    with pytest.raises(ValidationError):
        AiProviderEnvelope.model_validate(payload).to_intent()


def test_known_meal_missing_weight_preserves_identity_as_editable_unmeasured_item():
    payload = provider_payload(
        proposed(
            items=[
                wire_item(
                    grams=None,
                    quantity_source="unresolved",
                    uncertainty="image_unmeasured",
                    quantity_evidence=None,
                )
            ]
        )
    )
    intent = AiProviderEnvelope.model_validate(payload).to_intent()
    assert intent.intent == "meal" and intent.items[0].grams is None
    assert intent.items[0].identity_evidence == "cooked rice"
    assert not intent.unresolved and not intent.discovery_items


def discovery_payload(*, unknown=True, preparation="cooked", complete=True):
    known = wire_item()
    known.pop("basis")
    known.update(query="cooked rice", preparation="cooked")
    unknown_item = dict(known)
    unknown_item.update(
        food_version_id=None if unknown else 2,
        query="fried chicken with skin",
        preparation=preparation,
        identity_evidence="fried chicken with skin",
        quantity_evidence="200g fried chicken with skin",
        grams="200",
    )
    return {
        "result": {
            "kind": "discovery",
            "label": "dinner",
            "local_date": TODAY.isoformat(),
            "full_input_accounted_for": complete,
            "discovery_items": [known, unknown_item],
        }
    }


def test_complete_discovery_preserves_known_and_unknown_food_order_qualifiers_and_mass():
    intent = AiProviderEnvelope.model_validate(discovery_payload()).to_intent()
    assert intent.intent == "clarify" and intent.clarification_reason == "unknown_food"
    assert not intent.items
    assert [item.food_version_id for item in intent.discovery_items] == [1, None]
    assert [item.grams for item in intent.discovery_items] == ["150", "200"]
    assert intent.discovery_items[1].query == "fried chicken with skin"
    assert intent.local_date == TODAY


@pytest.mark.parametrize(
    "changes", [{"unknown": False}, {"preparation": "unspecified"}, {"complete": False}]
)
def test_discovery_still_requires_complete_preparation_and_a_genuinely_unknown_food(changes):
    with pytest.raises(ValidationError):
        AiProviderEnvelope.model_validate(discovery_payload(**changes)).to_intent()


def test_wire_schema_expresses_branch_fields_and_strict_required_objects():
    schema = _strict_schema()
    assert schema["type"] == "object" and "anyOf" not in schema
    assert len(schema["properties"]["result"]["anyOf"]) == 3
    assert schema["$defs"]["AiProviderMeal"]["properties"]["items"]["minItems"] == 1
    assert schema["$defs"]["AiProviderMeal"]["properties"]["full_input_accounted_for"]["enum"] == [
        True
    ]
    assert "items" not in schema["$defs"]["AiProviderClarification"]["properties"]
    assert "unresolved" not in schema["$defs"]["AiProviderMeal"]["properties"]
    for value in [schema, *schema["$defs"].values()]:
        if "properties" in value:
            assert value["additionalProperties"] is False
            assert set(value["required"]) == set(value["properties"])
    assert all(key not in json.dumps(schema) for key in ('"default"', '"const"', '"discriminator"'))


def test_provider_branches_share_a_discriminator_first_schema_prefix():
    schema = _strict_schema()
    prefix = ["kind", "label", "local_date", "full_input_accounted_for"]
    for branch, kind in (
        ("AiProviderMeal", "meal"),
        ("AiProviderClarification", "clarify"),
        ("AiProviderDiscovery", "discovery"),
    ):
        definition = schema["$defs"][branch]
        assert list(definition["properties"])[:4] == prefix
        assert definition["required"][:4] == prefix
        assert definition["properties"]["kind"]["enum"] == [kind]

    for payload in (
        provider_payload(proposed(items=[wire_item()])),
        clarification(),
        discovery_payload(),
    ):
        output = AiProviderEnvelope.model_validate(payload)
        assert list(output.model_dump()["result"])[:4] == prefix
        output.to_intent()


async def test_both_transports_accept_safe_clarification_and_reject_partial_meal():
    payload = clarification(clarification_reason="unsupported", full_input_accounted_for=True)
    assert (
        parse_completed_stream(stream(payload), expected_model="synthetic-model-v1").intent
        == "clarify"
    )
    adapter, client = adapter_with(lambda request: httpx.Response(200, json=completion(payload)))
    async with client:
        output = await adapter.complete(reviewed_route(), body={})
    assert output.proposal and output.proposal.intent == "clarify"
    partial = provider_payload(proposed(items=[wire_item()]))
    partial["result"]["unresolved"] = ["Missing preparation."]
    with pytest.raises(AiUnavailable):
        parse_completed_stream(stream(partial), expected_model="synthetic-model-v1")
    adapter, client = adapter_with(lambda request: httpx.Response(200, json=completion(partial)))
    async with client:
        output = await adapter.complete(reviewed_route(), body={})
    assert output.proposal is None and output.fault == "schema"


async def test_representative_thirteen_item_catalog_remains_within_existing_request_bound():
    foods = tuple(
        AiCatalogItem(
            food_version_id=index + 1,
            name=("Synthetic reviewed cooked rice " + "x" * 120)[:120],
            preparation="cooked",
            aliases=("a" * 80,),
        )
        for index in range(13)
    )
    async with httpx.AsyncClient() as client:
        adapter = ChatGPTPlanAdapter(Path("/synthetic/offline"), policy(), client=client)
        body = adapter.request_body(
            role="meal_text",
            text="150g cooked rice, 120g raw banana",
            catalog=foods,
            local_date=TODAY,
        )
    schema = body["text"]["format"]["schema"]
    user = body["input"][-1]["content"][0]["text"]
    assert len((SYSTEM_PROMPT + user + json.dumps(schema)).encode()) <= 12_000
    assert len(json.dumps(body).encode()) <= 12_000
    adapter, client = adapter_with(lambda request: pytest.fail("No transport needed"))
    async with client:
        adapter.request_body(
            reviewed_route(),
            text="150g cooked rice, 120g raw banana",
            catalog=foods,
            local_date=TODAY,
        )


def test_new_transport_does_not_accept_legacy_flat_shapes_silently():
    flat = {
        "status": "completed",
        "model": "synthetic-model-v1",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(proposed())}],
            }
        ],
    }
    event = {"type": "response.completed", "response": flat}
    with pytest.raises(AiUnavailable):
        parse_completed_stream(
            ("data: " + json.dumps(event) + "\n\n").encode(), expected_model="synthetic-model-v1"
        )


@pytest.mark.parametrize(
    "field", ["grams", "quantity_source", "uncertainty", "identity_evidence", "quantity_evidence"]
)
def test_new_provider_wire_requires_explicit_nullable_quantity_fields(field):
    payload = provider_payload(proposed(items=[wire_item()]))
    del payload["result"]["items"][0][field]
    with pytest.raises(ValidationError):
        AiProviderEnvelope.model_validate(payload)


def test_raw_and_cooked_weight_bases_keep_distinct_ids_with_exact_repeated_food_excerpts():
    text = "100g rice before cooking,150g rice after cooking"
    raw = wire_item(
        grams="100",
        identity_evidence="100g rice before cooking",
        quantity_evidence="100g rice before cooking",
    )
    cooked = wire_item(
        food_version_id=2,
        grams="150",
        identity_evidence="150g rice after cooking",
        quantity_evidence="150g rice after cooking",
    )
    intent = AiProviderEnvelope.model_validate(
        provider_payload(proposed(items=[raw, cooked]))
    ).to_intent()
    validated = validate_proposal(
        intent,
        catalog=(
            AiCatalogItem(food_version_id=1, name="Rice, raw", preparation="raw"),
            AiCatalogItem(food_version_id=2, name="Rice, cooked", preparation="cooked"),
        ),
        text=text,
        local_date=TODAY,
        role="meal_text",
    )
    assert validated is not None and validated.intent == "meal"
    assert [(item.food_version_id, item.grams) for item in validated.items] == [
        (1, "100"),
        (2, "150"),
    ]


def test_known_foods_with_all_weights_missing_remain_a_complete_editable_meal():
    intent = AiProviderEnvelope.model_validate(
        provider_payload(
            proposed(
                items=[
                    wire_item(
                        grams=None,
                        quantity_source="unresolved",
                        uncertainty="portion_missing",
                        identity_evidence="cooked rice",
                        quantity_evidence=None,
                    ),
                    wire_item(
                        food_version_id=2,
                        grams=None,
                        quantity_source="unresolved",
                        uncertainty="portion_missing",
                        identity_evidence="raw banana",
                        quantity_evidence=None,
                    ),
                ]
            )
        )
    ).to_intent()
    validated = validate_proposal(
        intent,
        catalog=(
            AiCatalogItem(food_version_id=1, name="Rice, cooked", preparation="cooked"),
            AiCatalogItem(food_version_id=2, name="Banana, raw", preparation="raw"),
        ),
        text="cooked rice, raw banana; edible weights unknown",
        local_date=TODAY,
        role="meal_text",
    )
    assert validated is not None and validated.intent == "meal"
    assert validated.full_input_accounted_for and not validated.unresolved
    assert len(validated.items) == 2
    assert all(
        item.grams is None and item.uncertainty == "portion_missing" for item in validated.items
    )


def test_invalid_date_is_judged_only_against_context_date_and_clarifies_without_future_fallback():
    context_date = date(2000, 1, 1)
    intent = AiProviderEnvelope.model_validate(
        clarification(
            local_date=context_date.isoformat(),
            clarification_reason="incomplete",
            unresolved=["Please give a valid date."],
        )
    ).to_intent()
    validated = validate_proposal(
        intent,
        catalog=(),
        text="Meal on 2001-01-01",
        local_date=context_date,
        role="meal_text",
    )
    assert validated is not None and validated.intent == "clarify"
    assert validated.local_date == context_date
    assert validated.unresolved == ("Please provide one valid meal date, today or in the past.",)


def test_explicit_decimal_edible_mass_and_date_use_the_authoritative_context():
    context_date = date(2099, 6, 15)
    meal_date = date(2099, 6, 14)
    clause = "peeled raw carrot 67.5g"
    intent = AiProviderEnvelope.model_validate(
        provider_payload(
            proposed(
                local_date=meal_date.isoformat(),
                items=[
                    wire_item(
                        grams="67.5",
                        identity_evidence=clause,
                        quantity_evidence=clause,
                    )
                ],
            )
        )
    ).to_intent()
    validated = validate_proposal(
        intent,
        catalog=(
            AiCatalogItem(
                food_version_id=1, name="Carrots, raw", preparation="raw", aliases=("carrot",)
            ),
        ),
        text=f"{meal_date.isoformat()}: {clause}",
        local_date=context_date,
        role="meal_text",
    )
    assert validated is not None and validated.intent == "meal"
    assert validated.local_date == meal_date
    assert validated.items[0].grams == "67.5"
    assert validated.items[0].quantity_source == "reported"
    assert validated.items[0].uncertainty == "none"


@pytest.mark.parametrize(
    "caption,questions",
    [
        (
            "A poultry portion. Food/cut, skin status and edible grams are unknown.",
            ["Which food and cut?", "Was skin consumed?", "What is the edible weight in grams?"],
        ),
        (
            "A cooked mixed casserole; its ingredients are unknown.",
            ["What were all ingredients, including sauces, oils and toppings?"],
        ),
    ],
)
def test_unknown_photo_identity_preserves_complete_questions_without_partial_meal(
    caption, questions
):
    intent = AiProviderEnvelope.model_validate(
        clarification(clarification_reason="food_identity", unresolved=questions)
    ).to_intent()
    validated = validate_proposal(
        intent,
        catalog=(),
        text=caption,
        local_date=TODAY,
        role="meal_photo",
    )
    assert validated is not None and validated.intent == "clarify"
    assert validated.clarification_reason == "food_identity"
    assert validated.unresolved == tuple(questions)
    assert not validated.items and not validated.discovery_items
