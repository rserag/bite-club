"""Model-selected excerpts cannot discard uncertainty in original synthetic input."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.ai.chatgpt_auth import save_credentials
from nutrition_bot.adapters.ai.chatgpt_plan import ChatGPTPlanAdapter
from nutrition_bot.adapters.database.schema_ai import ai_plan_invocations, ai_requests
from nutrition_bot.application.ai_service import AiService, create_ai_draft, validate_proposal
from nutrition_bot.domain.ai import AiCatalogItem, AiMealIntent, AiOutcome
from tests.helpers import message
from tests.test_ai_drafts import adapter_with, completion, manifest
from tests.test_chatgpt_plan_ai import credentials, policy, private_path, stream
from tests.test_chatgpt_plan_caps import plan_pair
from tests.test_telegram_meals import catalog as catalog

DAY = date(2026, 10, 9)


def checked(
    text, *, evidence, identity="rice", name="Rice", preparation="cooked", role="meal_text"
):
    proposal = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=DAY,
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "150",
                "basis": "Synthetic untrusted source",
                "identity_evidence": identity,
                "quantity_evidence": evidence,
            },
        ),
    )
    return validate_proposal(
        proposal,
        catalog=(AiCatalogItem(food_version_id=1, name=name, preparation=preparation),),
        text=text,
        local_date=DAY,
        role=role,
    )


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
def test_shaved_approximation_still_records_user_uncertainty(role):
    result = checked("about 150g rice", evidence="150g rice", role=role)
    assert result.items[0].grams == "150"
    assert result.items[0].quantity_source == "approximate"
    assert result.items[0].uncertainty == "user_approximation"


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize(
    "text", ["between 100 and 150g rice", "100-150g rice", "-150g rice", "−150g rice"]
)
def test_shaved_range_or_sign_cannot_become_a_measured_mass(role, text):
    result = checked(text, evidence="150g rice", role=role)
    assert result.items[0].grams is None
    assert result.items[0].quantity_source == "unresolved"


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize("text", ["unpeeled banana 150g", "banana still in its peel weighed 150g"])
def test_shaved_non_edible_weight_cannot_become_edible_mass(role, text):
    evidence = "banana 150g" if "unpeeled" in text else "banana still in its peel weighed 150g"
    result = checked(
        text, evidence=evidence, identity="banana", name="Banana", preparation="raw", role=role
    )
    assert result.items[0].grams is None
    assert result.items[0].quantity_source == "unresolved"


@pytest.mark.parametrize("source_state,catalog_state", [("raw", "cooked"), ("cooked", "raw")])
def test_shaved_identity_cannot_hide_raw_cooked_preparation(source_state, catalog_state):
    result = checked(
        f"150g {source_state} rice",
        evidence=f"150g {source_state} rice",
        preparation=catalog_state,
    )
    assert result.intent == "clarify" and result.clarification_reason == "preparation"
    assert not result.items


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize(
    "text",
    [
        "150g rice, my approximate weight estimate",
        "150g rice. That was my rough estimate.",
        "150g rice; that is my own rough weight estimate.",
    ],
)
def test_following_original_quantity_qualifier_is_preserved(role, text):
    result = checked(text, evidence="150g rice", role=role)
    assert result.items[0].grams == "150"
    assert result.items[0].quantity_source == "approximate"


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize(
    "text",
    ["rice 150g, or 180g", "rice 150g. I did not weigh it.", "rice 150g. I didn't weigh it."],
)
def test_following_original_alternative_or_no_measurement_cannot_be_shaved(role, text):
    result = checked(text, evidence="rice 150g", role=role)
    assert result.items[0].grams is None


def test_approximation_and_preparation_do_not_leak_from_another_food_clause():
    result = checked(
        "150g rice. About 100g raw chicken.",
        evidence="150g rice",
    )
    assert result.intent == "meal"
    assert result.items[0].quantity_source == "reported"


@pytest.mark.parametrize(
    "text", ["150g raw rice", "150g rice before cooking", "rice dry weight 150g"]
)
def test_original_pre_cooking_weight_cannot_use_cooked_record(text):
    result = checked(text, evidence=text, identity="rice")
    assert result.intent == "clarify" and result.clarification_reason == "preparation"


def test_original_post_cooking_weight_cannot_use_raw_record():
    result = checked("rice 150g after cooking", evidence="rice 150g", preparation="raw")
    assert result.intent == "clarify" and result.clarification_reason == "preparation"


def test_shaved_comma_preparation_continuation_still_rejects_wrong_record():
    result = checked("rice 150g, without oil or sauce", evidence="rice 150g")
    assert result.intent == "clarify" and result.clarification_reason == "preparation"


def test_arbitrary_other_food_evidence_cannot_authorize_reviewed_identity():
    result = checked("150g chicken", evidence="150g chicken", identity="chicken")
    assert result.intent == "clarify" and not result.items


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
def test_quantity_of_descriptor_ingredient_cannot_be_borrowed_by_food(role):
    result = checked(
        "cooked rice and 150g olive oil",
        evidence="150g olive oil",
        identity="cooked rice",
        name="Cooked rice without oil",
        role=role,
    )
    assert result.intent == "meal" and result.items[0].grams is None


@pytest.mark.parametrize(
    "name,text,evidence",
    [
        (
            "Chicken breast, meat only, cooked",
            "cooked chicken breast and turkey meat 150g",
            "turkey meat 150g",
        ),
        ("Sweet potato cooked", "sweet potato and sweet corn 150g", "sweet corn 150g"),
    ],
)
def test_generic_or_partial_compound_word_cannot_borrow_other_food_mass(name, text, evidence):
    identity = "cooked chicken breast" if name.startswith("Chicken") else "sweet potato"
    result = checked(text, evidence=evidence, identity=identity, name=name)
    assert result.intent == "meal" and result.items[0].grams is None


def test_separately_reviewed_alias_remains_a_valid_whole_food_anchor():
    value = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=DAY,
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "150",
                "basis": "Synthetic",
                "identity_evidence": "synthetic rice alias",
                "quantity_evidence": "synthetic rice alias 150g",
            },
        ),
    )
    result = validate_proposal(
        value,
        catalog=(
            AiCatalogItem(
                food_version_id=1,
                name="Oryza sativa",
                preparation="cooked",
                aliases=("synthetic rice alias",),
            ),
        ),
        text="synthetic rice alias 150g",
        local_date=DAY,
        role="meal_text",
    )
    assert result.intent == "meal" and result.items[0].grams == "150"


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
def test_whole_food_sentence_with_benign_preparation_preserves_reported_mass(role):
    text = "My chicken breast weighed 175g after cooking, with no oil or sauce."
    value = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=DAY,
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "175",
                "basis": "Synthetic",
                "identity_evidence": "chicken breast",
                "quantity_evidence": text,
            },
        ),
    )
    result = validate_proposal(
        value,
        catalog=(
            AiCatalogItem(
                food_version_id=1,
                name="Chicken breast, cooked without oil or sauce",
                preparation="cooked",
            ),
        ),
        text=text,
        local_date=DAY,
        role=role,
    )
    assert result.items[0].grams == "175"
    assert result.items[0].quantity_source == "reported"


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize("prefix,expected_source", [("", "reported"), ("about ", "approximate")])
@pytest.mark.parametrize(
    "query", ["Fried chicken breast with skin", "Fried skin-on chicken breast"]
)
def test_source_supported_skin_on_unknown_food_keeps_mass_for_review(
    role, prefix, expected_source, query
):
    text = f"Fried chicken breast with skin weighed cooked at {prefix}150g."
    value = AiMealIntent(
        intent="clarify",
        label="Meal",
        local_date=DAY,
        items=(),
        clarification_reason="unknown_food",
        full_input_accounted_for=True,
        discovery_items=(
            {
                "food_version_id": None,
                "query": query,
                "preparation": "cooked",
                "grams": "150",
                "quantity_evidence": text,
                "identity_evidence": "Fried chicken breast with skin",
            },
        ),
    )
    result = validate_proposal(value, catalog=(), text=text, local_date=DAY, role=role)
    assert result.discovery_items[0].grams == "150"
    assert result.discovery_items[0].quantity_source == expected_source
    assert result.discovery_items[0].query == query


@pytest.mark.parametrize(
    "text",
    [
        "Fried chicken breast with skin and salad 150g",
        "Fried chicken breast with skin, salad 150g",
        "Fried chicken breast with skin and bones weighed 150g",
    ],
)
def test_skin_preparation_cannot_lend_other_food_or_gross_mass(text):
    result = checked(
        text,
        evidence=text,
        identity="Fried chicken breast with skin",
        name="Chicken breast, fried with skin",
    )
    assert result.items[0].grams is None


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
def test_impossible_date_empty_clarification_is_preserved_without_assigning_a_meal(role):
    value = AiMealIntent(
        intent="clarify",
        label="Meal",
        local_date=DAY,
        items=(),
        discovery_items=(),
        full_input_accounted_for=False,
        clarification_reason="incomplete",
        unresolved=("2026-02-30 is invalid. What date did you eat the rice?",),
    )
    result = validate_proposal(
        value, catalog=(), text="100g cooked rice on 2026-02-30", local_date=DAY, role=role
    )
    assert result is not None and result.intent == "clarify"
    assert not result.items and not result.discovery_items
    assert "valid" in result.unresolved[0] and "date" in result.unresolved[0]


def test_impossible_date_cannot_begin_unknown_food_discovery_or_authorize_guessed_date():
    value = AiMealIntent(
        intent="clarify",
        label="Meal",
        local_date=DAY,
        items=(),
        full_input_accounted_for=True,
        clarification_reason="unknown_food",
        discovery_items=(
            {
                "query": "Mango",
                "preparation": "raw",
                "grams": "100",
                "quantity_evidence": "mango100g",
            },
        ),
    )
    assert (
        validate_proposal(
            value, catalog=(), text="mango100g on 2026-02-30", local_date=DAY, role="meal_text"
        )
        is None
    )


async def test_invalid_date_receipt_marks_date_unresolved_and_uses_fixed_local_guidance(store):
    value = AiMealIntent(
        intent="clarify",
        label="Meal",
        local_date=DAY,
        items=(),
        discovery_items=(),
        full_input_accounted_for=False,
        clarification_reason="incomplete",
        unresolved=("Synthetic unsafe raw model prose",),
    )
    result = validate_proposal(
        value, catalog=(), text="rice on 2026-02-30", local_date=DAY, role="meal_text"
    )
    outcome = AiOutcome(
        request_key="synthetic-invalid-date", role="meal_text", status="clarify", proposal=result
    )
    async with store.write() as connection:
        reply = await create_ai_draft(
            connection,
            outcome,
            message(1, "synthetic invalid date").message,
            action_key="synthetic-invalid-date",
            reference=datetime.now(UTC),
        )
    assert "date unresolved" in reply.text and "valid meal date" in reply.text
    assert str(DAY) not in reply.text and "unsafe raw model prose" not in reply.text
    assert "Nothing has been saved" in reply.text


@pytest.mark.parametrize(
    "text", ["today and yesterday rice", "tomorrow rice", "rice on 2026-02-30"]
)
def test_uncertain_date_clarification_must_not_invent_an_alternate_date(text):
    value = AiMealIntent(
        intent="clarify",
        label="Meal",
        local_date=DAY - timedelta(days=1),
        items=(),
        full_input_accounted_for=False,
        clarification_reason="incomplete",
    )
    assert validate_proposal(value, catalog=(), text=text, local_date=DAY, role="meal_text") is None


@pytest.mark.parametrize("field", ["identity_evidence", "quantity_evidence"])
def test_photo_nonverbatim_evidence_stays_clarified_or_clamped(field):
    value = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=DAY,
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "150",
                "basis": "Synthetic",
                "identity_evidence": "cooked rice",
                "quantity_evidence": "150g cooked rice",
            },
        ),
    )
    bad = value.model_copy(
        update={"items": (value.items[0].model_copy(update={field: "150g cooked rice 油"}),)}
    )
    result = validate_proposal(
        bad,
        catalog=(AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),),
        text="150g cooked rice",
        local_date=DAY,
        role="meal_photo",
    )
    if field == "identity_evidence":
        assert result.intent == "clarify" and not result.items
    else:
        assert result.items[0].grams is None


@pytest.mark.parametrize(
    "text,evidence",
    [
        ("rice with chicken 150g", "rice with chicken 150g"),
        ("rice with no oil, chicken 150g", "rice with no oil, chicken 150g"),
        ("rice with no oil chicken 150g", "rice with no oil chicken 150g"),
        ("rice 150g with chicken", "rice 150g with chicken"),
        (
            "rice 150g, with no oil or sauce and chicken",
            "rice 150g, with no oil or sauce and chicken",
        ),
    ],
)
def test_preparation_clause_cannot_hide_cross_food_quantity_evidence(text, evidence):
    result = checked(text, evidence=evidence, name="Rice cooked without oil")
    assert result.items[0].grams is None


@pytest.mark.parametrize(
    "text,name,identity",
    [
        (
            "Boiled potato including edible skin, no butter: 150g weighed cooked.",
            "Potato, boiled, with edible skin, cooked without butter",
            "potato",
        ),
        (
            "Chicken breast, meat only, stewed without added fat, weighed after stewing at 150g.",
            "Chicken breast, meat only, stewed without added fat",
            "Chicken breast",
        ),
        (
            "Enriched white rice, cooked in water without added fat, weighed cooked at 150g.",
            "Rice, white, enriched, cooked without added fat",
            "rice",
        ),
        (
            "Boiled potatoes including edible skin, without added ingredients. "
            "Their cooked edible weight was measured at 150g.",
            "Potato, boiled, with edible skin, cooked without added ingredients",
            "potatoes",
        ),
    ],
)
def test_comma_separated_preparation_and_measurement_clauses_keep_exact_mass(text, name, identity):
    result = checked(text, evidence=text, identity=identity, name=name)
    assert result.intent == "meal" and result.items[0].grams == "150"


@pytest.mark.parametrize("other_food", ["oil", "salt", "milk", "turkey meat"])
def test_separate_ingredient_clause_does_not_become_a_measurement_reference(other_food):
    result = checked(f"rice, {other_food} 150g", evidence=f"rice, {other_food} 150g")
    assert result.items[0].grams is None


@pytest.mark.parametrize("separator", ["\n", "; "])
def test_food_and_unambiguous_mass_reference_on_separate_lines_still_binds(separator):
    text = f"cooked rice{separator}weighed 150g after cooking"
    result = checked(text, evidence=text)
    assert result.items[0].grams == "150" and result.items[0].quantity_source == "reported"


@pytest.mark.parametrize("separator", ["\n", "; "])
def test_other_food_on_separate_line_cannot_lend_its_mass(separator):
    text = f"rice{separator}chicken 150g"
    result = checked(text, evidence=text)
    assert result.items[0].grams is None


def test_raw_preparation_on_following_line_is_not_shaved_by_identity_quote():
    text = "rice\nraw weight 150g"
    result = checked(text, evidence=text)
    assert result.intent == "clarify" and result.clarification_reason == "preparation"


@pytest.mark.parametrize("separator", ["\n", ", ", "; "])
def test_missing_legacy_identity_evidence_cannot_omit_adjacent_raw_preparation(separator):
    text = f"rice{separator}raw weight 150g"
    result = checked(text, evidence=text, identity=None)
    assert result.intent == "clarify" and result.clarification_reason == "preparation"


def test_legacy_ambiguous_repeated_food_clauses_cannot_authorize_preparation():
    text = "rice\nraw weight 150g\nrice\ncooked weight 150g"
    result = checked(text, evidence="rice\nraw weight 150g", identity=None)
    assert result.intent == "clarify" and result.clarification_reason == "preparation"


def test_shaved_prefix_numeric_comma_range_cannot_authorize_final_endpoint():
    result = checked("between 100, 150g rice", evidence="150g rice")
    assert result.items[0].grams is None


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
def test_decimal_comma_mass_preserves_exact_decimal_conversion(role):
    text = "Raw rice weighed 80,25 grams before cooking."
    value = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=DAY,
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "80.25",
                "basis": "Synthetic",
                "identity_evidence": "rice",
                "quantity_evidence": "rice weighed 80,25 grams",
            },
        ),
    )
    result = validate_proposal(
        value,
        catalog=(AiCatalogItem(food_version_id=1, name="Rice", preparation="raw"),),
        text=text,
        local_date=DAY,
        role=role,
    )
    assert result.items[0].grams == "80.25" and result.items[0].quantity_source == "reported"


@pytest.mark.parametrize("offset,phrase", [(1, "one day"), (2, "two days"), (7, "7 days")])
def test_bounded_explicit_days_ago_preserves_event_date(offset, phrase):
    value = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=DAY - timedelta(days=offset),
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "150",
                "basis": "Synthetic",
                "identity_evidence": "rice",
                "quantity_evidence": "rice 150g",
            },
        ),
    )
    result = validate_proposal(
        value,
        catalog=(AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),),
        text=f"{phrase} ago, rice 150g",
        local_date=DAY,
        role="meal_text",
    )
    assert result.local_date == DAY - timedelta(days=offset)


@pytest.mark.parametrize("role", ["meal_text", "meal_photo"])
@pytest.mark.parametrize("quantity", ["150g", "about 150g", "-150g", "100-150g"])
def test_valid_iso_date_is_independent_of_original_signed_or_ranged_mass(role, quantity):
    value = AiMealIntent(
        intent="meal",
        label="Meal",
        local_date=date(2026, 10, 3),
        full_input_accounted_for=True,
        items=(
            {
                "food_version_id": 1,
                "grams": "150",
                "basis": "Synthetic",
                "identity_evidence": "rice",
                "quantity_evidence": "150g cooked rice",
            },
        ),
    )
    result = validate_proposal(
        value,
        catalog=(AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),),
        text=f"On 2026-10-03 I ate {quantity} cooked rice",
        local_date=DAY,
        role=role,
    )
    assert result.local_date == date(2026, 10, 3)
    if quantity.startswith(("-", "100-")):
        assert result.items[0].grams is None
    else:
        assert result.items[0].grams == "150"
        assert result.items[0].quantity_source == (
            "approximate" if quantity.startswith("about") else "reported"
        )


@pytest.mark.parametrize("phrase", ["eight days ago", "0 days ago", "many days ago"])
def test_unbounded_or_ambiguous_days_ago_cannot_authorize_model_date(phrase):
    assert checked(f"{phrase}, rice 150g", evidence="rice 150g") is None


@pytest.mark.parametrize("provider", ["openrouter", "chatgpt"])
@pytest.mark.parametrize("evaluation", [True, False])
async def test_raw_model_capture_is_evaluation_only_and_cleared_on_replay(
    store, tmp_path, provider, evaluation
):
    value = {
        "intent": "meal",
        "label": "Meal",
        "local_date": DAY.isoformat(),
        "full_input_accounted_for": True,
        "items": [
            {
                "food_version_id": 1,
                "grams": "150",
                "basis": "Synthetic",
                "identity_evidence": "rice",
                "quantity_evidence": "rice",
                "quantity_source": "reported",
                "uncertainty": "none",
            }
        ],
    }
    prefix = "eval:source-context:" if evaluation else None
    key = (prefix or "synthetic:") + provider
    catalog = (AiCatalogItem(food_version_id=1, name="Rice", preparation="cooked"),)
    if provider == "openrouter":
        adapter, client = adapter_with(lambda request: httpx.Response(200, json=completion(value)))
        service = AiService(
            store,
            manifest(),
            adapter,
            evaluation_prefix=prefix,
            evaluation_catalog=catalog if evaluation else None,
        )
    else:
        path = private_path(tmp_path)
        save_credentials(path, credentials())

        def transport(request):
            if request.method == "GET":
                return httpx.Response(
                    200, json={"models": [{"slug": "synthetic-model-v1", "visibility": "list"}]}
                )
            return httpx.Response(200, content=stream(value))

        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        adapter = ChatGPTPlanAdapter(path, policy(), client=client)
        service = AiService(
            store,
            plan_adapter=adapter,
            evaluation_prefix=prefix,
            evaluation_catalog=catalog if evaluation else None,
        )

    # Inject the same bounded synthetic context for ordinary calls, without opting
    # those calls into evaluation or retaining their raw model responses.
    async def synthetic_catalog(text=""):
        return catalog

    service._catalog = synthetic_catalog
    try:
        outcome = await service.interpret(request_key=key, text="rice", local_date=DAY)
        assert outcome.proposal.items[0].grams is None
        if evaluation:
            assert service.evaluation_raw_proposal.items[0].grams == "150"
        else:
            assert service.evaluation_raw_proposal is None
        assert await service.interpret(request_key=key, text="rice", local_date=DAY) == outcome
        assert service.evaluation_raw_proposal is None
    finally:
        await client.aclose()


@pytest.mark.parametrize("extra_date_offset", [-1, 0, 1])
async def test_dated_subscription_supplement_preserves_history_and_separate_bot_pool(
    store, catalog, tmp_path, monkeypatch, extra_date_offset
):
    fixed = datetime.combine(DAY, datetime.min.time(), UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr("nutrition_bot.application.ai_service.datetime", Clock)
    # Keep quota, privacy review and mocked meal dates on the same synthetic clock.
    monkeypatch.setattr("nutrition_bot.adapters.ai.chatgpt_plan.datetime", Clock)
    monkeypatch.setattr("tests.test_ai_drafts.TODAY", DAY)
    monkeypatch.setattr("tests.test_chatgpt_plan_ai.TODAY", DAY)
    history = (("eval:synthetic:old-done", "done"), ("eval:synthetic:old-unknown", "unknown"))
    async with store.write() as connection:
        for key, state in history:
            await connection.execute(
                sa.insert(ai_requests).values(
                    request_key=key, role="meal_text", state=state, created_at=1
                )
            )
            await connection.execute(
                sa.insert(ai_plan_invocations).values(
                    request_key=key, day=DAY.isoformat(), state=state, created_at=1
                )
            )
    async with plan_pair(
        store,
        tmp_path,
        daily_invocation_limit=1,
        daily_evaluation_invocation_limit=1,
        extra_evaluation_date=DAY + timedelta(days=extra_date_offset),
        extra_evaluation_invocation_limit=2,
    ) as (bot, evaluation, calls, _):
        first = await evaluation.interpret(
            request_key="eval:synthetic:new-one", text="synthetic rice", local_date=DAY
        )
        assert first.status == ("ready" if extra_date_offset == 0 else "quota")
        second = await evaluation.interpret(
            request_key="eval:synthetic:new-two", text="synthetic rice", local_date=DAY
        )
        assert second.status == "quota"
        assert (
            await bot.interpret(request_key="synthetic-bot", text="synthetic rice", local_date=DAY)
        ).status == "ready"
        assert (
            await evaluation.interpret(
                request_key="eval:synthetic:old-unknown", text="synthetic rice", local_date=DAY
            )
        ).status == "unknown"
        assert calls.count("POST") == (2 if extra_date_offset == 0 else 1)
    async with store.engine.connect() as connection:
        for key, state in history:
            row = (
                (
                    await connection.execute(
                        sa.select(ai_plan_invocations).where(
                            ai_plan_invocations.c.request_key == key
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert row["state"] == state and row["created_at"] == 1
