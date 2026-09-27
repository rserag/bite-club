"""Typed, temporary meal proposals. Amounts remain unapproved until a button action."""

import math
from datetime import date, datetime
from decimal import localcontext
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from nutrition_bot.domain.food import MAX_INTEGER, FrozenModel, exact_decimal
from nutrition_bot.domain.recipe_portions import RecipeShare

DRAFT_TTL_SECONDS = 7 * 24 * 60 * 60
Identifier = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
DraftState = Literal["open", "saved", "cancelled", "expired"]


class DraftError(ValueError):
    """An actionable message that is safe to show in Telegram."""


class PlannedItem(FrozenModel):
    recipe_share: RecipeShare | None = None
    food_version_id: Identifier
    edible_milligrams: int | None = Field(default=None, strict=True, ge=1, le=50_000_000)
    original_quantity: str | None = Field(default=None, min_length=1, max_length=40)
    original_unit: str | None = Field(default=None, min_length=1, max_length=2)
    estimate_basis: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_quantity(self) -> "PlannedItem":
        amounts = (self.edible_milligrams, self.original_quantity, self.original_unit)
        if self.recipe_share is not None and any(value is None for value in amounts):
            raise ValueError("Recipe shares require their complete batch ingredient quantity.")
        if all(value is None for value in amounts):
            if self.estimate_basis is not None:
                raise ValueError("An unresolved portion cannot have an estimate basis.")
            return self
        if any(value is None for value in amounts):
            raise ValueError(
                "Provide the complete amount and unit, or leave the amount unresolved."
            )
        assert self.original_unit is not None and self.original_quantity is not None
        factors = {"g": 1000, "kg": 1_000_000, "mg": 1}
        if self.original_unit.casefold() not in factors:
            raise ValueError("Enter the proposed edible amount in g, kg or mg.")
        with localcontext() as context:
            context.prec = 50
            quantity = (
                exact_decimal(self.original_quantity) * factors[self.original_unit.casefold()]
            )
        if quantity != self.edible_milligrams:
            raise ValueError("The quantity must match the proposed edible mass exactly.")
        return self


class DraftContent(FrozenModel):
    label: str = Field(min_length=1, max_length=120)
    local_date: date
    timezone: str = Field(min_length=1, max_length=100)
    consumed_at: float
    source_chat_id: Identifier
    source_message_id: Identifier
    items: tuple[PlannedItem, ...] = Field(min_length=1, max_length=10)
    target_meal_id: Identifier | None = None
    target_revision_id: Identifier | None = None

    @field_validator("consumed_at", mode="before")
    @classmethod
    def validate_timestamp_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Choose a valid meal time.")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> "DraftContent":
        if (self.target_meal_id is None) != (self.target_revision_id is None):
            raise ValueError("A correction must identify both the meal and its revision.")
        try:
            zone = ZoneInfo(self.timezone)
            if not math.isfinite(self.consumed_at):
                raise ValueError
            if datetime.fromtimestamp(self.consumed_at, zone).date() != self.local_date:
                raise ValueError
        except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
            raise ValueError("Choose a valid meal time, calendar date and time zone.") from None
        return self


class MealDraft(FrozenModel):
    id: Identifier
    revision: Identifier
    state: DraftState
    content: DraftContent | None
    last_user_activity_at: float
    saved_meal_id: Identifier | None = None
