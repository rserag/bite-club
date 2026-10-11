"""AI proposes reviewed-catalog identities and quantities, never nutrition facts."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from nutrition_bot.domain.food import MAX_INTEGER, FrozenModel

AiRole = Literal["meal_text", "meal_photo"]
MONTHLY_MICRO_USD_LIMIT = 10_000_000
MAX_AI_TEXT_BYTES = 4000
MAX_AI_IMAGE_BYTES = 2_000_000
MAX_AI_RESPONSE_BYTES = 65_536
MAX_AI_STREAM_BYTES = 524_288
AI_PROMPT_VERSION = "meal-extraction-v10"
AI_SCHEMA_VERSION = "meal-proposal-v4"
AI_VALIDATION_VERSION = "meal-evidence-v9"
AiFailureCategory = Literal[
    "auth",
    "policy",
    "catalog",
    "network",
    "timeout",
    "http",
    "usage_limit",
    "incomplete",
    "refusal",
    "oversized",
    "schema",
    "usage",
    "pricing",
    "interrupted",
    "download",
]


@dataclass
class AiCallMetrics:
    """Content-free observations of one provider attempt; absent usage stays unknown."""

    model: str | None = None
    prompt_version: str = AI_PROMPT_VERSION
    schema_version: str = AI_SCHEMA_VERSION
    reasoning_effort: str = "default"
    auth_ms: int | None = None
    model_catalog_ms: int | None = None
    inference_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    charged_micro_usd: int | None = None
    inference_sent: bool = False
    failure_category: AiFailureCategory | None = None


def known_token_count(value: object) -> int | None:
    """Accept only exact, bounded integers supplied by a complete provider response."""
    return value if type(value) is int and 0 <= value <= MAX_INTEGER else None


def known_token_detail(value: object, *, total: int | None) -> int | None:
    """A detailed usage count is credible only within its known parent total."""
    count = known_token_count(value)
    return count if total is not None and count is not None and count <= total else None


class AiUnavailable(ValueError):
    """Fixed, safe status suitable for user-facing manual-entry fallback."""


class AiCatalogItem(FrozenModel):
    food_version_id: Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
    name: str = Field(min_length=1, max_length=500)
    preparation: Literal["raw", "cooked", "as_sold", "as_prepared"]
    aliases: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = Field(
        default=(),
        max_length=1,
    )


QuantitySource = Literal["reported", "approximate", "unresolved", "unspecified"]
QuantityUncertainty = Literal[
    "none", "user_approximation", "image_unmeasured", "portion_missing", "unspecified"
]
ClarificationReason = Literal[
    "unknown_food", "preparation", "food_identity", "portion", "unsupported", "incomplete", "image"
]


class AiQuantity(FrozenModel):
    """Bounded proposal metadata; raw model prose never becomes quantity provenance."""

    grams: str | None = Field(default=None, pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$")
    quantity_source: QuantitySource = "unspecified"
    uncertainty: QuantityUncertainty = "unspecified"
    quantity_evidence: str | None = Field(default=None, min_length=1, max_length=120)
    identity_evidence: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def positive_mass(self) -> "AiQuantity":
        if self.grams is not None:
            from decimal import Decimal

            if not 0 < Decimal(self.grams) <= 50_000:
                raise ValueError("The proposed portion is outside the supported range.")
        if self.grams is None and self.quantity_source in {"reported", "approximate"}:
            raise ValueError("A reported quantity needs an amount.")
        if self.grams is not None and self.quantity_source == "unresolved":
            raise ValueError("An unresolved amount cannot contain a proposed mass.")
        if self.quantity_source == "reported" and self.uncertainty != "none":
            raise ValueError("A reported mass must retain its reported provenance.")
        if self.quantity_source == "approximate" and self.uncertainty != "user_approximation":
            raise ValueError("Only an explicit user approximation may propose an estimated mass.")
        return self


class AiMealItem(AiQuantity):
    food_version_id: Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
    basis: str = Field(min_length=1, max_length=160)


class AiDiscoveryItem(AiQuantity):
    """Ordered unresolved food identity, suitable only for the reviewed source workflow."""

    food_version_id: Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)] | None = None
    query: str = Field(min_length=1, max_length=80)
    preparation: Literal["raw", "cooked", "as_sold", "as_prepared", "unspecified"]

    @model_validator(mode="after")
    def food_query(self) -> "AiDiscoveryItem":
        if (
            not any(character.isalpha() for character in self.query)
            or re.fullmatch(r"[\w\s,.'’%()/-]+", self.query) is None
            or re.search(
                r"\b(?:calories?|kcal|nutrients?|sql|execute|ignore|instructions?|"
                r"database|http|https|approve|save)\b",
                self.query,
                re.IGNORECASE,
            )
            or any(character in self.query for character in "\n\r\t")
        ):
            raise ValueError("Discovery accepts only a short food name.")
        return self


def quantity_basis(item: AiQuantity, role: AiRole) -> str | None:
    """Only fixed local wording may enter the immutable quantity ledger."""
    if item.grams is None or item.quantity_source == "reported":
        return None
    if item.quantity_source == "approximate":
        return "User-described approximate mass; AI interpretation requires review."
    origin = "photo" if role == "meal_photo" else "text"
    return f"AI {origin} interpreted mass; source uncertainty requires review."


def uncertainty_message(reason: ClarificationReason) -> str:
    return {
        "unknown_food": "One or more foods need catalog discovery and source review.",
        "preparation": (
            "Please clarify any missing preparation details (raw, cooked or packaged), "
            "including whether the weight was measured before or after cooking."
        ),
        "food_identity": (
            "Please clarify any missing food names/cuts, skin or bones, preparation "
            "and edible weight in grams; include sauces, oils, toppings and sides."
        ),
        "portion": "Please provide each food's edible weight in grams.",
        "unsupported": "This route supports meal descriptions and meal photos only.",
        "incomplete": "Please provide the complete meal, preparation and edible amounts.",
        "image": "This image cannot safely establish the meal. Describe the foods manually.",
    }[reason]


class AiMealIntent(FrozenModel):
    """No arbitrary query, correction, nutrient, save, or tool-call fields exist."""

    intent: Literal["meal", "clarify"]
    label: str = Field(min_length=1, max_length=60)
    local_date: date
    items: tuple[AiMealItem, ...] = Field(default=(), max_length=10)
    unresolved: tuple[str, ...] = Field(default=(), max_length=10)
    full_input_accounted_for: bool = Field(strict=True)
    clarification_reason: ClarificationReason = "incomplete"
    discovery_items: tuple[AiDiscoveryItem, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def complete_or_clarify(self) -> "AiMealIntent":
        if self.intent == "meal" and (
            not self.items
            or self.unresolved
            or self.discovery_items
            or not self.full_input_accounted_for
        ):
            raise ValueError("Partial interpretations cannot become meal proposals.")
        if self.intent == "clarify" and self.items:
            raise ValueError("Clarification must not propose a partial meal.")
        if self.discovery_items and (
            self.intent != "clarify"
            or self.clarification_reason != "unknown_food"
            or not self.full_input_accounted_for
            or not any(item.food_version_id is None for item in self.discovery_items)
            or any(item.preparation == "unspecified" for item in self.discovery_items)
        ):
            raise ValueError("Only complete unknown-food interpretations can start discovery.")
        if any(not 1 <= len(part) <= 160 for part in self.unresolved):
            raise ValueError("Clarification fields must be short.")
        return self


class AiProviderMealItem(AiMealItem):
    """New requests require explicit provenance; old stored drafts remain readable."""

    grams: str | None = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$")
    quantity_source: Literal["reported", "approximate", "unresolved"]
    uncertainty: Literal["none", "user_approximation", "image_unmeasured", "portion_missing"]
    quantity_evidence: str | None = Field(min_length=1, max_length=120)
    identity_evidence: str | None = Field(min_length=1, max_length=120)


class AiProviderDiscoveryItem(AiDiscoveryItem):
    food_version_id: Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)] | None
    grams: str | None = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,3})?$")
    quantity_source: Literal["reported", "approximate", "unresolved"]
    uncertainty: Literal["none", "user_approximation", "image_unmeasured", "portion_missing"]
    quantity_evidence: str | None = Field(min_length=1, max_length=120)
    identity_evidence: str | None = Field(min_length=1, max_length=120)
    preparation: Literal["raw", "cooked", "as_sold", "as_prepared"]


class AiProviderComplete(FrozenModel):
    # Structured outputs preserve schema property order. Every union branch
    # must expose its kind first so choosing a key does not choose the branch.
    kind: Literal["meal", "discovery"]
    label: str = Field(min_length=1, max_length=60)
    local_date: date
    full_input_accounted_for: Literal[True]

    @field_validator("full_input_accounted_for", mode="before")
    @classmethod
    def exact_complete_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("A complete proposal requires an exact true completeness flag.")
        return value


class AiProviderMeal(AiProviderComplete):
    kind: Literal["meal"]
    items: tuple[AiProviderMealItem, ...] = Field(min_length=1, max_length=10)


class AiProviderClarification(FrozenModel):
    kind: Literal["clarify"]
    label: str = Field(min_length=1, max_length=60)
    local_date: date
    full_input_accounted_for: bool = Field(strict=True)
    clarification_reason: ClarificationReason
    unresolved: tuple[Annotated[str, Field(min_length=1, max_length=160)], ...] = Field(
        max_length=10
    )


class AiProviderDiscovery(AiProviderComplete):
    kind: Literal["discovery"]
    discovery_items: tuple[AiProviderDiscoveryItem, ...] = Field(min_length=1, max_length=10)


class AiProviderEnvelope(FrozenModel):
    """Provider wire branches exclude contradictory fields before semantic validation.

    The object root and nested anyOf work with strict structured outputs. Conversion
    always passes through the unchanged durable meal-intent safety validator.
    """

    result: AiProviderMeal | AiProviderClarification | AiProviderDiscovery

    def to_intent(self) -> AiMealIntent:
        result = self.result
        if isinstance(result, AiProviderMeal):
            return AiMealIntent.model_validate(
                {
                    "intent": "meal",
                    "label": result.label,
                    "local_date": result.local_date,
                    "full_input_accounted_for": result.full_input_accounted_for,
                    "items": [item.model_dump() for item in result.items],
                }
            )
        if isinstance(result, AiProviderDiscovery):
            return AiMealIntent.model_validate(
                {
                    "intent": "clarify",
                    "label": result.label,
                    "local_date": result.local_date,
                    "full_input_accounted_for": result.full_input_accounted_for,
                    "clarification_reason": "unknown_food",
                    "discovery_items": [item.model_dump() for item in result.discovery_items],
                }
            )
        return AiMealIntent.model_validate(
            {
                "intent": "clarify",
                "label": result.label,
                "local_date": result.local_date,
                "full_input_accounted_for": result.full_input_accounted_for,
                "clarification_reason": result.clarification_reason,
                "unresolved": result.unresolved,
            }
        )


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
