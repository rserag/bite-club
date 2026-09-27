from dataclasses import replace
from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from nutrition_bot.adapters.database.checkins import mark_food_day
from nutrition_bot.adapters.database.foods import publish_reviewed_food
from nutrition_bot.adapters.database.nutrient_references import install_bundled
from nutrition_bot.adapters.database.schema_supplements import nutrient_reference_sets as sets
from nutrition_bot.adapters.database.schema_supplements import nutrient_reference_values as refs
from nutrition_bot.application.nutrient_conversation import handle_nutrient_message
from nutrition_bot.domain.food import NutrientInput, ReviewedFoodInput
from nutrition_bot.domain.nutrient_gaps import FoodValue, Week, screen, summarize
from nutrition_bot.domain.nutrient_references import GROUPS, VERSION, bundled_values, content_hash
from nutrition_bot.domain.supplement_reports import Exposure
from tests.helpers import message
from tests.test_supplement_foundation import add_action
from tests.test_supplement_reports import report_text, seed_product
from tests.test_telegram_meals import process

M = 1_000_000
END = date(2023, 11, 12)
DAYS = [END - timedelta(days=13 - i) for i in range(14)]


def week(**kwargs):
    return replace(Week(5, 100 * M, 0, 0, 5, 5, (), False, 0), **kwargs)


def status(first=None, second=None, **kwargs):
    return screen(
        (first or week(), second or week()),
        code="magnesium",
        kind="RDA",
        target=100 * M,
        scope="total",
        **kwargs,
    )


def test_repeated_trigger_and_exact_boundary():
    assert status() == "possible_intake_gap"
    assert status(second=week(observed=400 * M)) == "no_repeated_trigger"
    assert status(second=week(observed=400 * M - 1)) == "possible_intake_gap"
    assert status(second=week(supplement=300 * M)) == "no_repeated_trigger"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"complete_days": 4}, "insufficient_days"),
        ({"complete_days": 0, "observed": None}, "insufficient_days"),
        ({"entries": 10, "known_entries": 9, "unknown_foods": ("salt",)}, "coverage_uncertain"),
        ({"entries": 10, "known_entries": 8}, "coverage_uncertain"),
        ({"uncertain_supplements": True}, "coverage_uncertain"),
        ({"observed": None}, "coverage_uncertain"),
    ],
)
def test_gates_apply_to_each_week(changes, expected):
    assert status(first=week(**changes)) == expected
    assert status(second=week(**changes)) == expected


@pytest.mark.parametrize(
    "code,kind", [("sodium", "RDA"), ("potassium", "AI"), ("magnesium", "UL"), ("sodium", "limit")]
)
def test_no_gap_from_ai_or_limits(code, kind):
    assert (
        screen((week(), week()), code=code, kind=kind, target=M, scope="total")
        == "not_gap_reference"
    )


@pytest.mark.parametrize(
    "scope,form", [("supplement", ""), ("total", "oxide"), ("fortified_and_supplement", "")]
)
def test_form_or_source_specific_rda_is_not_misapplied(scope, form):
    assert (
        screen((week(), week()), code="magnesium", kind="RDA", target=M, scope=scope, form=form)
        == "unsupported_reference"
    )


def test_observed_imputed_unknown_and_incomplete_days_stay_separate():
    complete = set(DAYS[:5])
    food = [
        FoodValue(DAYS[0], "known", 100 * M, "manual_reviewed", True),
        FoodValue(DAYS[1], "imputed", 900 * M, "imputed"),
        FoodValue(DAYS[2], "unknown", None, "unknown"),
        FoodValue(DAYS[6], "excluded", 5000 * M, "source_reported"),
    ]
    dose = Exposure(DAYS[0], 1, 1, "magnesium", "Magnesium", "magnesium", "mg", 10 * M, None)
    result = summarize(complete, food, [dose, replace(dose, day=DAYS[6], amount=9999 * M)], "mg")
    assert result.observed == 100 * M
    assert result.imputed == 900 * M
    assert result.supplement == 10 * M
    assert result.unknown_foods == ("imputed", "unknown")
    assert (result.known_entries, result.entries, result.estimated_entries) == (1, 3, 1)
    assert not result.eligible


def test_only_explicit_complete_empty_days_can_be_zero():
    assert summarize(set(), [], [], "mg").observed is None
    empty = summarize(set(DAYS[:5]), [], [], "mg")
    assert empty.observed == 0 and empty.eligible


def test_dataset_age_sex_types_units_and_scopes():
    values = bundled_values()
    lookup = {(v.reference_group, v.nutrient_code, v.kind): v for v in values}
    assert len(values) == len(lookup) == 136
    assert {v.reference_group for v in values} == set(GROUPS)
    assert lookup["female-51-70", "calcium", "RDA"].amount_scaled == 1200 * M
    assert lookup["male-51-70", "calcium", "RDA"].amount_scaled == 1000 * M
    assert lookup["male-71+", "vitamin_d", "RDA"].amount_scaled == 20 * M
    assert lookup["female-19-30", "magnesium", "RDA"].amount_scaled == 310 * M
    assert lookup["male-31-50", "magnesium", "RDA"].amount_scaled == 420 * M
    assert lookup["female-31-50", "iron", "RDA"].amount_scaled == 18 * M
    assert lookup["female-51-70", "iron", "RDA"].amount_scaled == 8 * M
    for group in GROUPS:
        assert lookup[group, "magnesium", "UL"].applicability == "supplement"
        assert lookup[group, "vitamin_b12", "RDA"].amount_scaled == 2_400_000
        assert lookup[group, "vitamin_b12", "RDA"].unit == "ug"
        assert lookup[group, "sodium", "limit"].amount_scaled == 2300 * M
        assert (group, "sodium", "UL") not in lookup
        assert (group, "vitamin_b12", "UL") not in lookup
    assert all("https://www.canada.ca/" in v.note for v in values)
    assert len(content_hash(values)) == 64


async def test_install_idempotent_sealed_and_version_conflicts_rejected(store):
    async with store.write() as conn:
        rid = await install_bundled(conn)
        assert await install_bundled(conn) == rid
        assert await conn.scalar(sa.select(sa.func.count()).select_from(refs)) == 136
    with pytest.raises(sa.exc.IntegrityError):
        async with store.write() as conn:
            await conn.execute(
                sa.update(refs).where(refs.c.reference_set_id == rid).values(amount_scaled=1)
            )
    async with store.engine.connect() as conn:
        assert await conn.scalar(sa.select(sets.c.version)) == VERSION


async def test_install_refuses_conflicting_same_version(store):
    async with store.write() as conn:
        await conn.execute(
            sa.insert(sets).values(
                framework="US_DRI",
                version=VERSION,
                name="synthetic conflict",
                source_url="https://example.invalid",
                reviewed_at=1,
                content_sha256="0" * 64,
                sealed=False,
            )
        )
        with pytest.raises(ValueError, match="integrity"):
            await install_bundled(conn)


async def test_reference_choice_is_explicit_and_blank_days_not_zero(service, store):
    help_reply = await process(service, store, message(1, "/nutrients"))
    assert "No group is selected automatically" in help_reply["payload"]["text"]
    async with store.engine.connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count()).select_from(sets)) == 0
    await process(service, store, message(2, "/nutrients references"))
    result = await report_text(service, store, 10, "/nutrients review 1 male-31-50")
    assert "W1=0/7, W2=0/7" in result
    assert "F unknown" in result and "Possible intake gap" not in result
    assert "Insufficient complete days" in result
    values = await report_text(service, store, 30, "/nutrients values 1 female-51-70")
    assert "calcium · RDA · 1200 mg/day" in values
    assert "magnesium · UL · 350 mg/day · supplement" in values
    assert "CDRR, not UL" in values
    for line in values.splitlines():
        assert len(line.encode("utf-16-le")) // 2 < 4096


@pytest.mark.parametrize(
    "command",
    [
        "/nutrients review",
        "/nutrients review 1 male-31-50 2023-11-14",
        "/nutrients review 1 male-31-50 2023-11-13",
        "/nutrients review 1 male-31-50 0001-01-07",
        "/nutrients review 999999999999999999999999 male-31-50",
        "/nutrients references page 0",
        "/nutrients values 1 male-31-50 extra",
        '/nutrients review "',
    ],
)
async def test_invalid_requests_cannot_assess_gap(store, command):
    async with store.write() as conn:
        result = await handle_nutrient_message(conn, command, today=date(2023, 11, 14))
        assert result.kind == "nutrient_help"


async def seed_weeks(service, store, *, missing=False):
    async with store.write() as conn:
        await install_bundled(conn)
        await publish_reviewed_food(
            conn,
            ReviewedFoodInput(
                name="testfood",
                preparation="cooked",
                source_reference="Synthetic test only",
                source_license="Synthetic",
                nutrients=(
                    NutrientInput(code="energy", amount="100", unit="kcal"),
                    NutrientInput(code="magnesium", amount=None if missing else "100", unit="mg"),
                ),
            ),
        )
    for i, day in enumerate(DAYS):
        await process(service, store, message(i + 1, f"/meal {day} 100g testfood"))
        async with store.write() as conn:
            key = await add_action(conn, 5000 + i)
            await mark_food_day(conn, day, "complete", action_key=key)


async def test_real_ledger_repeated_gap_supplements_and_correction(service, store):
    await seed_weeks(service, store)
    result = await report_text(service, store, 100, "/nutrients review 1 male-31-50")
    assert "W1=7/7, W2=7/7" in result
    assert "magnesium · RDA 420 mg/day · total: Possible intake gap" in result
    assert "F 100; S 0; C 100 mg/complete day" in result
    assert "calcium · RDA 1000 mg/day · total: Coverage uncertain" in result
    async with store.write() as conn:
        for n, day in enumerate(DAYS):
            await seed_product(conn, 1000 + n * 2, day=day, amount=300 * M)
    result = await report_text(service, store, 200, "/nutrients review 1 male-31-50")
    assert "magnesium · RDA 420 mg/day · total: No repeated below-80% trigger" in result
    assert "F 100; S 300; C 400 mg/complete day" in result
    # Corrected food invalidates the day's earlier completeness marker.
    await process(service, store, message(300, f"/meal {DAYS[0]} 100g testfood"))
    result = await report_text(service, store, 400, "/nutrients review 1 male-31-50")
    assert "W1=6/7, W2=7/7" in result


async def test_complete_days_with_unknown_food_values_block_screen(service, store):
    await seed_weeks(service, store, missing=True)
    result = await report_text(service, store, 100, "/nutrients review 1 male-31-50")
    assert "W1=7/7, W2=7/7" in result
    assert "Possible intake gap" not in result
    assert "magnesium: unknown/imputed food value — testfood" in result
