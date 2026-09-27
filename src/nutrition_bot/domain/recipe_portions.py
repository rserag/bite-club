"""Exact rational recipe consumption, independent of drafts and persistence."""

from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, model_validator

from nutrition_bot.domain.food import MAX_INTEGER, FrozenModel

RecipeIdentifier = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]


class RecipeError(ValueError):
    """A safe recipe validation message for Telegram."""


class RecipeShare(FrozenModel):
    recipe_id: RecipeIdentifier
    version_id: RecipeIdentifier
    version_number: RecipeIdentifier
    name: str = Field(min_length=1, max_length=60)
    ingredient_index: int = Field(strict=True, ge=0, le=9)
    unit: Literal["g", "serving"]
    total_units: int = Field(strict=True, ge=1, le=50_000_000)
    portion_units: int = Field(strict=True, ge=1, le=50_000_000)
    yield_estimate_basis: str | None = Field(default=None, min_length=1, max_length=300)
    portion_estimate_basis: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_fraction(self) -> "RecipeShare":
        maximum = 50_000_000 if self.unit == "g" else 1_000_000
        if not self.portion_units <= self.total_units <= maximum:
            raise ValueError("A recipe portion must fit within its defined batch.")
        return self

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.portion_units, self.total_units)

    def scaled(self, factor: Decimal) -> "RecipeShare":
        exponent = factor.as_tuple().exponent if isinstance(factor, Decimal) else None
        if (
            not isinstance(factor, Decimal)
            or not factor.is_finite()
            or not 0 < factor <= 100
            or not isinstance(exponent, int)
            or exponent < -3
        ):
            raise RecipeError("Use a positive scale up to 100 with at most three decimals.")
        with localcontext() as context:
            context.prec = 50
            portion = self.portion_units * factor
        if portion != portion.to_integral_value() or not 1 <= portion <= self.total_units:
            raise RecipeError(
                "The scaled portion must fit the batch in exact milligrams "
                "or thousandths of a serving."
            )
        return RecipeShare.model_validate(self.model_dump() | {"portion_units": int(portion)})

    def amount_text(self) -> str:
        with localcontext() as context:
            context.prec = 50
            amount = Decimal(self.portion_units) / 1000
        return f"{amount:f} {'g' if self.unit == 'g' else 'serving(s)'}"


def ingredient_grams(share: RecipeShare, batch_milligrams: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return Decimal(batch_milligrams) * share.portion_units / (1000 * share.total_units)
