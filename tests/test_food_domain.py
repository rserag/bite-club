from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from nutrition_bot.domain.food import (
    MAX_INTEGER,
    NutrientInput,
    PortionInput,
    ReviewedFoodInput,
    convert_unit,
    exact_decimal,
    grams_to_milligrams,
    per_100g_scaled,
    scale_nutrient,
)


def test_unknown_zero_and_fractional_scaling_remain_distinct():
    assert scale_nutrient(None, 250_000) is None
    assert scale_nutrient(0, 250_000) == 0
    assert scale_nutrient(1_230_001, 33_333) == Decimal("0.40999623333")
    assert per_100g_scaled(Decimal("10.5"), "g", "g", Decimal("30")) == 35_000_000


def test_mass_units_and_energy_dimensions():
    assert convert_unit(Decimal("0.0025"), "mg", "ug") == Decimal("2.5")
    assert per_100g_scaled(Decimal("0.12"), "g", "mg", Decimal("40")) == 300_000_000
    with pytest.raises(ValueError, match="Incompatible"):
        convert_unit(Decimal(1), "kcal", "g")
    with pytest.raises(ValidationError):
        NutrientInput(code="sodium", amount="1", unit="salt_g")


def test_round_once_half_even_with_bounded_storage():
    assert per_100g_scaled(Decimal("0.0000005"), "g", "g", Decimal(100)) == 0
    assert per_100g_scaled(Decimal("0.0000015"), "g", "g", Decimal(100)) == 2
    assert per_100g_scaled(Decimal("0.0000005"), "g", "g", Decimal(50)) == 1
    with pytest.raises(ValueError, match="storage range"):
        per_100g_scaled(Decimal("1000000000"), "g", "ug", Decimal("0.001"))
    assert scale_nutrient(MAX_INTEGER, 1) == Decimal("92233720.36854775807")


@pytest.mark.parametrize(
    "value", [0.1, True, "NaN", "Infinity", "-1", "1e-1000000", "0e1000000", "1000000001"]
)
def test_invalid_source_numbers_rejected(value):
    with pytest.raises(ValueError):
        exact_decimal(value)


def test_calculations_ignore_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 3
        assert grams_to_milligrams(Decimal("123.456")) == 123456
        assert per_100g_scaled(Decimal("1.234567"), "g", "mg", Decimal("12.345")) == 10_000_542_730
        assert scale_nutrient(1_230_001, 33_333) == Decimal("0.40999623333")


@pytest.mark.parametrize("grams", ["0", "-1", "1.0001"])
def test_mass_never_silently_rounded(grams):
    with pytest.raises(ValueError):
        grams_to_milligrams(Decimal(grams))


def test_portions_default_to_estimates_and_do_not_imply_logging_approval():
    portion = PortionInput(
        label="one item", grams="45", original_measure="one item", source="synthetic average"
    )
    assert portion.is_estimate is True
    with pytest.raises(ValidationError):
        portion.grams = Decimal("50")


def test_duplicate_unknown_only_and_unexpected_food_fields_rejected():
    base = dict(
        name="Synthetic food",
        preparation="raw",
        source_reference="fixture",
        source_license="synthetic",
        nutrients=[dict(code="protein", amount="2", unit="g")],
    )
    for overrides in (
        {"nutrients": base["nutrients"] * 2},
        {"nutrients": [dict(code="protein", amount=None, unit="g")]},
        {"basis_grams": "0"},
        {"invented_calories": "100"},
        {"preparation": "probably cooked"},
    ):
        with pytest.raises(ValidationError):
            ReviewedFoodInput.model_validate({**base, **overrides})
