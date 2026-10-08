"""AI proposes reviewed-catalog identities and quantities, never nutrition facts."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from nutrition_bot.domain.food import MAX_INTEGER, FrozenModel

AiRole = Literal["meal_text", "meal_photo"]
MONTHLY_MICRO_USD_LIMIT = 10_000_000
MAX_AI_TEXT_BYTES = 4000
MAX_AI_IMAGE_BYTES = 2_000_000
MAX_AI_RESPONSE_BYTES = 65_536


class AiUnavailable(ValueError):
    """Fixed, safe status suitable for user-facing manual-entry fallback."""


class AiCatalogItem(FrozenModel):
    food_version_id: Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
    name: str = Field(min_length=1, max_length=120)
    preparation: Literal["raw", "cooked", "as_sold", "as_prepared"]


class AiMealItem(FrozenModel):
    food_version_id: Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
    grams: str | None = Field(default=None, pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$")
    basis: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def positive_mass(self) -> "AiMealItem":
        if self.grams is not None:
            from decimal import Decimal

            if not 0 < Decimal(self.grams) <= 50_000:
                raise ValueError("The proposed portion is outside the supported range.")
        return self


class AiMealIntent(FrozenModel):
    """No arbitrary query, correction, nutrient, save, or tool-call fields exist."""

    intent: Literal["meal", "clarify"]
    label: str = Field(min_length=1, max_length=60)
    local_date: date
    items: tuple[AiMealItem, ...] = Field(default=(), max_length=10)
    unresolved: tuple[str, ...] = Field(default=(), max_length=10)
    full_input_accounted_for: bool = Field(strict=True)

    @model_validator(mode="after")
    def complete_or_clarify(self) -> "AiMealIntent":
        if self.intent == "meal" and (
            not self.items or self.unresolved or not self.full_input_accounted_for
        ):
            raise ValueError("Partial interpretations cannot become meal proposals.")
        if self.intent == "clarify" and self.items:
            raise ValueError("Clarification must not propose a partial meal.")
        if any(not 1 <= len(part) <= 160 for part in self.unresolved):
            raise ValueError("Clarification fields must be short.")
        return self


class AiOutcome(FrozenModel):
    request_key: str = Field(min_length=1, max_length=128)
    role: AiRole
    proposal: AiMealIntent | None = None
    status: Literal["ready", "clarify", "disabled", "budget", "quota", "unavailable", "unknown"]

    @model_validator(mode="after")
    def coherent(self) -> "AiOutcome":
        if (self.status in {"ready", "clarify"}) != (self.proposal is not None):
            raise ValueError("AI outcome must have its matching proposal.")
        if self.proposal and (self.status == "ready") != (self.proposal.intent == "meal"):
            raise ValueError("AI outcome does not match its validated intent.")
        return self

    @property
    def manual_message(self) -> str:
        return {
            "clarify": "I could not account for the complete meal. Choose the foods in /foods "
            "and send their amounts in grams; nothing has been saved.",
            "disabled": "AI is not enabled yet. Use your saved foods, favorites, or measured "
            "amounts in grams; nothing has been saved.",
            "budget": "This month's AI budget is unavailable for another request. Your saved "
            "foods, favorites and reports still work; nothing has been saved.",
            "quota": "Today's ChatGPT plan request allowance has been reached. Enter the meal "
            "manually; nothing has been saved.",
            "unknown": "The AI request result is unknown, so it will not be retried. "
            "Enter the meal manually; nothing has been saved.",
            "unavailable": "AI could not safely prepare this meal. Choose the foods in /foods "
            "and enter their amounts in grams; nothing has been saved.",
            "ready": "Review the complete draft before approving it.",
        }[self.status]
