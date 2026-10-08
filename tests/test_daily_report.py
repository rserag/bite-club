"""Report parsing and presentation limits; all quantities here are synthetic."""

from dataclasses import replace
from datetime import date

import pytest

from nutrition_bot.adapters.database.checkins import FoodDayStatus
from nutrition_bot.adapters.database.goals import TargetPlanSnapshot
from nutrition_bot.application.daily_report import DailyRequestError, parse_request, render_daily
from nutrition_bot.domain.daily import CORE_NUTRIENT_ORDER, DailyMeal, DailyNutrient, DailyTotals

DAY = date(2026, 1, 15)


def nutrient(code="protein", *, amount=150_000, known=1, total=2, unit="g"):
    return DailyNutrient(
        code,
        code.replace("_", " ").title(),
        unit,
        amount,
        known,
        total,
        (("source_reported", known),),
    )


def totals(*nutrients):
    return DailyTotals(
        DAY,
        (DailyMeal(1, 3, "Lunch"),),
        2,
        nutrients,
        (("manual_reviewed", 1), ("usda", 1)),
        (("measured", 2),),
        ("UTC",),
    )


def target():
    return TargetPlanSnapshot(
        id=3,
        goal_id=1,
        effective_from=date(2026, 1, 1),
        energy_kcal=2000,
        protein_grams=100,
        fat_grams=60,
        carbohydrate_grams=265,
        energy_range_low_kcal=None,
        energy_range_high_kcal=None,
        source="manual",
    )


@pytest.mark.parametrize(
    ("text", "day", "short"),
    [
        ("/today", DAY, False),
        ("/today full", DAY, False),
        ("/today short", DAY, True),
        ("/today yesterday", date(2026, 1, 14), False),
        ("/today short yesterday", date(2026, 1, 14), True),
        ("/today 2026-01-01 short", date(2026, 1, 1), True),
        ("/TODAY Today", DAY, False),
        ("how am I doing today?", DAY, False),
        ("Show today’s totals!", DAY, False),
        ("today", DAY, False),
    ],
)
def test_report_request(text, day, short):
    result = parse_request(text, today=DAY)
    assert result.day == day and result.short is short


@pytest.mark.parametrize(
    "text",
    [
        "/today future",
        "/today 2026-01-16",
        "/today 2026-02-30",
        "/today 2026-1-1",
        "/today 20260101",
        "/today short full",
        "/today yesterday today",
        "/today full full",
        "/today yesterday 100g rice",
        "/today 0000-01-01",
        "/today short extra",
    ],
)
def test_invalid_request_is_not_partly_interpreted(text):
    with pytest.raises(DailyRequestError, match="Nothing was changed"):
        parse_request(text, today=DAY)


def test_yesterday_before_minimum_date_is_safe():
    with pytest.raises(DailyRequestError):
        parse_request("/today yesterday", today=date.min)


@pytest.mark.parametrize("text", ["/todayx", "I ate 100g rice", "", "today I ate 100g rice"])
def test_unrelated_input_is_not_intercepted(text):
    assert parse_request(text, today=DAY) is None


def test_unknown_partial_zero_and_rounding_are_distinct():
    report = render_daily(
        totals(
            nutrient("energy", amount=1, unit="kcal"),
            nutrient(),
            nutrient("fat", amount=0, known=2),
            nutrient("calcium", amount=None, known=0, unit="mg"),
            nutrient("vitamin_b12", amount=1, unit="ug"),
        ),
        timezone="UTC",
    )
    assert "Energy: <1 kcal known · data 1/2 foods" in report
    assert "Protein: 0.2 g known · data 1/2 foods" in report
    assert "Fat: 0.0 g · data 2/2 foods" in report
    assert "Calcium: unknown · data 0/2 foods" in report
    assert "Vitamin B12: <0.1 ug known · data 1/2 foods" in report
    assert "reviewed manual 1; USDA 1" in report
    assert "Portions (entries): measured 2" in report
    assert "source-reported 5" in report
    assert "does not measure day completeness" in report
    assert "No calorie or macro target was effective" in report


def test_short_view_still_discloses_partial_coverage():
    report = render_daily(totals(nutrient(), nutrient("calcium")), timezone="UTC", short=True)
    assert "Protein: 0.2 g · partial sum, data 1/2 foods" in report
    assert "Calcium:" not in report
    assert "Details: /today 2026-01-15 full" in report
    assert "Food sources" not in report and "Portions" not in report
    assert "Have you logged everything eaten on 2026-01-15?" in report


def test_short_complete_nutrient_data_does_not_claim_complete_food_log():
    value = totals(
        nutrient("energy", amount=550_000_000, known=2, unit="kcal"),
        nutrient("protein", amount=55_000_000, known=2),
        nutrient("carbohydrate", amount=80_000_000, known=2),
        nutrient("fat", amount=0, known=2),
        nutrient("fiber", amount=1_500_000, known=2),
    )
    report = render_daily(value, timezone="UTC", short=True)
    assert "Energy: 550 kcal\nProtein: 55.0 g\nCarbohydrate: 80.0 g\nFat: 0.0 g" in report
    assert "data " not in report
    assert "All food logged for this date" not in report
    assert "Have you logged everything eaten on 2026-01-15?" in report
    assert "Known" not in report and "Coverage" not in report
    assert len(report.splitlines()) <= 11


def test_short_targets_keep_partial_progress_unknown_and_over_target_explicit():
    report = render_daily(
        totals(
            nutrient("energy", amount=2100_000_000, known=2, unit="kcal"),
            nutrient("protein", amount=55_000_000),
            nutrient("carbohydrate", amount=None, known=0),
            nutrient("fat", amount=0, known=2),
        ),
        timezone="UTC",
        short=True,
        target=target(),
    )
    assert "Energy: 2100 kcal / 2000 kcal target · 100 kcal over" in report
    assert (
        "Protein: 55.0 g / 100 g target · partial sum, data 1/2 foods · remaining unknown" in report
    )
    assert "Carbohydrate: unknown / 265 g target · data 0/2 foods" in report
    assert "Fat: 0.0 g / 60 g target · 60.0 g remaining" in report
    assert "Progress is provisional" in report


def test_short_estimated_mixed_meal_keeps_approximation_beside_summary():
    value = replace(
        totals(nutrient("energy", amount=550_000_000, known=2, unit="kcal")),
        quantity_counts=(("approved_estimate", 1), ("measured", 1)),
    )
    report = render_daily(value, timezone="UTC", short=True)
    assert "Recorded nutrition · approximate portions\nEnergy: 550 kcal" in report
    assert "Portions (entries)" not in report
    detailed = render_daily(value, timezone="UTC")
    assert "approved estimate 1; measured 1" in detailed


@pytest.mark.parametrize("short", [True, False])
def test_stale_completion_and_unresolved_drafts_both_remain_visible(short):
    report = render_daily(
        totals(nutrient("energy", amount=500_000_000, known=2, unit="kcal")),
        timezone="UTC",
        short=short,
        target=target(),
        status=FoodDayStatus("unknown", 4, 1, True),
    )
    assert "Food log changed after it was marked all food logged" in report
    assert "1 unresolved meal draft(s) are outside these totals" in report
    assert "Remaining targets withheld until unresolved meal drafts" in report
    assert "1500 kcal remaining" not in report
    assert "Have you logged everything eaten on 2026-01-15?" in report
    assert "Food log status: unknown" not in report


def test_short_historical_date_is_explicit_and_timezone_explanation_is_in_details():
    value = totals(nutrient("energy", amount=500_000_000, known=2, unit="kcal"))
    report = render_daily(value, timezone="America/New_York", short=True, target=target())
    assert "Daily food log · 2026-01-15" in report
    assert "2000 kcal target" in report
    assert "Have you logged everything eaten on 2026-01-15?" in report
    assert "timezone" not in report and "Date requested" not in report
    assert "recorded in another timezone" in render_daily(value, timezone="America/New_York")


@pytest.mark.parametrize("short", [True, False])
def test_complete_empty_day_preserves_explicit_zero_and_remaining_targets(short):
    empty = replace(totals(), meals=(), item_count=0, source_counts=(), quantity_counts=())
    report = render_daily(
        empty,
        timezone="UTC",
        short=short,
        target=target(),
        status=FoodDayStatus("complete", 1, 0, False),
    )
    assert "explicitly marked complete, so recorded intake is zero" in report
    assert "Energy 2000 kcal remaining" in report
    assert "All food logged for this date" in report
    assert "Have you logged" not in report


def test_empty_day_is_not_zero_or_a_completed_day():
    empty = replace(totals(), meals=(), item_count=0, source_counts=(), quantity_counts=())
    report = render_daily(empty, timezone="UTC")
    assert "Intake is unknown, not zero" in report
    assert "0 kcal" not in report


def test_timezone_change_is_visible_without_rebucketing_history():
    report = render_daily(totals(nutrient()), timezone="America/New_York")
    assert "Daily food log · 2026-01-15" in report
    assert "recorded in another timezone" in report


def test_extension_display_limits_do_not_misrepresent_totals():
    report = render_daily(
        totals(*(nutrient(code) for code in (*CORE_NUTRIENT_ORDER, *(f"n{i}" for i in range(12))))),
        timezone="UTC",
    )
    assert "6 additional registered nutrients are not displayed" in report
    assert "Vitamin C:" in report


def test_unicode_large_values_and_large_diary_fit_telegram_utf16_limit():
    size = 2**63 - 1
    codes = (*CORE_NUTRIENT_ORDER, *(f"n{i}" for i in range(200)))
    values = tuple(
        DailyNutrient(code, "😀" * 500, "mg", size * size, size, size, (("😀" * 64, size),))
        for code in codes
    )
    large = DailyTotals(
        DAY,
        tuple(DailyMeal(size, size, "😀" * 120) for _ in range(100)),
        size,
        values,
        tuple(("😀" * 64, size) for _ in range(5)),
        tuple(("😀" * 64, size) for _ in range(5)),
        ("UTC",),
    )
    for short in (True, False):
        report = render_daily(large, timezone="America/Argentina/ComodRivadavia", short=short)
        assert len(report.encode("utf-16-le")) // 2 < 4096
        if short:
            assert "data " not in report
        else:
            assert report.count("data ") >= 14
    assert "additional registered nutrients are not displayed" in report
    assert "+97 other meals included in every total" in report


def test_content_labels_cannot_inject_lines_into_report():
    value = replace(totals(nutrient()), meals=(DailyMeal(1, 1, "Lunch\nInjected headline"),))
    report = render_daily(value, timezone="UTC")
    assert "M1r1 · Lunch Injected headline" in report
