"""Report parsing and presentation limits; all quantities here are synthetic."""

from dataclasses import replace
from datetime import date

import pytest

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
    assert "Protein: 0.2 g known · data 1/2 foods" in report
    assert "Calcium:" not in report
    assert "Full report: /today 2026-01-15" in report
    assert "Food sources" in report and "Portions" in report


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
        assert report.count("data ") >= (5 if short else 14)
    assert "additional registered nutrients are not displayed" in report
    assert "+97 other meals included in every total" in report


def test_content_labels_cannot_inject_lines_into_report():
    value = replace(totals(nutrient()), meals=(DailyMeal(1, 1, "Lunch\nInjected headline"),))
    report = render_daily(value, timezone="UTC")
    assert "M1r1 · Lunch Injected headline" in report
