"""Immutable batch definitions and deterministic ingredient portions."""

import re
import unicodedata
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from nutrition_bot.domain.drafts import Identifier, PlannedItem
from nutrition_bot.domain.food import FrozenModel
from nutrition_bot.domain.recipe_portions import RecipeError as RecipeError
from nutrition_bot.domain.recipe_portions import RecipeShare


class RecipeDefinition(FrozenModel):
    items: tuple[PlannedItem, ...] = Field(min_length=1, max_length=10)
    unit: Literal["g", "serving"]
    total_units: int = Field(strict=True, ge=1, le=50_000_000)
    estimate_basis: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_batch(self) -> "RecipeDefinition":
        if self.unit == "serving" and self.total_units > 1_000_000:
            raise ValueError("Define at most 1000 servings per batch.")
        if any(
            item.edible_milligrams is None or item.recipe_share is not None for item in self.items
        ):
            raise ValueError(
                "Recipe ingredients must have resolved amounts and cannot contain nested recipes."
            )
        return self


class RecipeSnapshot(FrozenModel):
    id: Identifier
    version_id: Identifier
    version_number: Identifier
    name: str = Field(min_length=1, max_length=60)
    archived: bool
    definition: RecipeDefinition


def recipe_display_name(name: str) -> str:
    if not isinstance(name, str) or any(
        unicodedata.category(c).startswith("C") or unicodedata.category(c) in {"Zl", "Zp"}
        for c in name
    ):
        raise RecipeError("Use a recipe name without control characters or newlines.")
    value = " ".join(unicodedata.normalize("NFC", name).split())
    if not 1 <= len(value) <= 60 or len(value.casefold()) > 60:
        raise RecipeError("Use a recipe name between 1 and 60 characters.")
    if any(c in value for c in "=|") or re.fullmatch(r"R[0-9]+(?:v[0-9]+)?", value, re.IGNORECASE):
        raise RecipeError(
            "Choose a name without '=', '|' or a reserved recipe reference such as R1v2."
        )
    if re.match(r"(?:today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s|$)", value, re.IGNORECASE):
        raise RecipeError("Keep dates out of the recipe name.")
    return value


def normalize_recipe_name(name: str) -> str:
    return recipe_display_name(name).casefold()


def combined_estimate_basis(
    ingredient: str | None, yield_basis: str | None, portion: str | None
) -> str | None:
    parts = []
    for label, value in (("Ingredient", ingredient), ("Yield", yield_basis), ("Portion", portion)):
        if value:
            clean = " ".join(value.split())
            parts.append(f"{label}: {clean if len(clean) <= 84 else clean[:83] + '…'}")
    return "; ".join(parts) or None


def recipe_portions(
    snapshot: RecipeSnapshot, portion_units: int, *, estimate_basis: str | None = None
) -> tuple[PlannedItem, ...]:
    result = []
    try:
        for index, item in enumerate(snapshot.definition.items):
            share = RecipeShare(
                recipe_id=snapshot.id,
                version_id=snapshot.version_id,
                version_number=snapshot.version_number,
                name=snapshot.name,
                ingredient_index=index,
                unit=snapshot.definition.unit,
                total_units=snapshot.definition.total_units,
                portion_units=portion_units,
                yield_estimate_basis=snapshot.definition.estimate_basis,
                portion_estimate_basis=estimate_basis,
            )
            result.append(
                PlannedItem.model_validate(
                    item.model_dump()
                    | {
                        "recipe_share": share,
                        "estimate_basis": combined_estimate_basis(
                            item.estimate_basis, snapshot.definition.estimate_basis, estimate_basis
                        ),
                    }
                )
            )
    except ValidationError:
        raise RecipeError(
            "Choose a valid portion within the recipe's batch, "
            "with a short estimate basis if needed."
        ) from None
    return tuple(result)
