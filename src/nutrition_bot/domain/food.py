"""Reviewed food inputs and exact nutrient arithmetic. No nutrient estimates."""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

NUTRIENT_SCALE = 1_000_000
MAX_INTEGER = 2**63 - 1
Unit = Literal["kcal", "g", "mg", "ug"]
Preparation = Literal["raw", "cooked", "as_sold", "as_prepared", "unspecified"]


def exact_decimal(value: object) -> Decimal:
    # Python floats have already lost the original decimal representation.
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise ValueError("Use a decimal string or integer")
    try:
        number = Decimal(value)
    except ArithmeticError:
        raise ValueError("Invalid decimal") from None
    if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
        raise ValueError("Value must be finite and between zero and one billion")
    exponent = number.as_tuple().exponent
    if not isinstance(exponent, int) or not -12 <= exponent <= 9:
        raise ValueError("Decimal exponent is outside the supported range")
    return number


Amount = Annotated[Decimal, BeforeValidator(exact_decimal)]
PositiveAmount = Annotated[Amount, Field(gt=0)]
Text = Annotated[str, Field(min_length=1, max_length=500)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class NutrientDefinition(FrozenModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    name: Text
    unit: Unit
    definition: Text


class NutrientInput(FrozenModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    amount: Amount | None
    unit: Unit
    note: Text | None = None


class PortionInput(FrozenModel):
    label: Text
    grams: PositiveAmount
    original_measure: Text
    source: Text
    is_estimate: bool = True


class ReviewedFoodInput(FrozenModel):
    name: Text
    preparation: Preparation
    brand: Text | None = None
    source_reference: Text
    source_url: Text | None = None
    source_license: Text
    basis_grams: PositiveAmount = Decimal("100")
    nutrients: tuple[NutrientInput, ...] = Field(min_length=1, max_length=200)
    portions: tuple[PortionInput, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_record(self) -> "ReviewedFoodInput":
        codes = [item.code for item in self.nutrients]
        labels = [item.label.casefold() for item in self.portions]
        if len(codes) != len(set(codes)) or len(labels) != len(set(labels)):
            raise ValueError("Duplicate nutrient or portion")
        if not any(item.amount is not None for item in self.nutrients):
            raise ValueError("At least one known nutrient is required")
        grams_to_milligrams(self.basis_grams)
        for portion in self.portions:
            grams_to_milligrams(portion.grams)
        return self


def grams_to_milligrams(grams: Decimal) -> int:
    with localcontext() as context:
        context.prec = 50
        value = exact_decimal(grams) * 1000
        if value <= 0 or value != value.to_integral_value():
            raise ValueError("Mass must be positive with at most three decimal places in grams")
        return int(value)


def milligrams_to_grams(milligrams: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return Decimal(milligrams) / 1000


def convert_unit(amount: Decimal, source: Unit, target: Unit) -> Decimal:
    if source == target:
        return amount
    mass_units = {"g": Decimal(1), "mg": Decimal("0.001"), "ug": Decimal("0.000001")}
    if source not in mass_units or target not in mass_units:
        raise ValueError("Incompatible nutrient units")
    with localcontext() as context:
        context.prec = 50
        return amount * mass_units[source] / mass_units[target]


def per_100g_scaled(amount: Decimal, unit: Unit, canonical: Unit, basis: Decimal) -> int:
    """Quantize once into millionths of the canonical unit; preserve source separately."""
    amount = exact_decimal(amount)
    grams_to_milligrams(basis)
    with localcontext() as context:
        context.prec = 50
        normalized = convert_unit(amount, unit, canonical) * 100 / basis
        stored = (normalized * NUTRIENT_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)
        if stored > MAX_INTEGER:
            raise ValueError("Nutrient amount exceeds storage range")
        return int(stored)


def scale_nutrient(amount_scaled: int | None, edible_milligrams: int) -> Decimal | None:
    """Keep unknown distinct from known zero; round only when presenting or storing a log."""
    if type(edible_milligrams) is not int or edible_milligrams <= 0:
        raise ValueError("Edible mass must be positive integer milligrams")
    if amount_scaled is None:
        return None
    if type(amount_scaled) is not int or not 0 <= amount_scaled <= MAX_INTEGER:
        raise ValueError("Invalid stored nutrient amount")
    with localcontext() as context:
        context.prec = 50
        return Decimal(amount_scaled) * edible_milligrams / (NUTRIENT_SCALE * 100_000)
