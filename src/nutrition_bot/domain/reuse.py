"""Exact meal reuse. Saved preferences never carry an earlier estimate's consent."""

import re
import unicodedata
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from pydantic import Field, ValidationError

from nutrition_bot.domain.drafts import Identifier, PlannedItem
from nutrition_bot.domain.food import FrozenModel, milligrams_to_grams

if TYPE_CHECKING:
    from nutrition_bot.adapters.database.meals import MealSnapshot


class ReuseError(ValueError):
    """A safe, actionable message for Telegram."""


class FavoriteSnapshot(FrozenModel):
    id: Identifier
    version_id: Identifier
    version_number: Identifier
    name: str = Field(min_length=1, max_length=60)
    items: tuple[PlannedItem, ...] = Field(min_length=1, max_length=10)
    archived: bool


def favorite_display_name(name: str) -> str:
    if not isinstance(name, str) or any(
        unicodedata.category(c).startswith("C") or unicodedata.category(c) in {"Zl", "Zp"}
        for c in name
    ):
        raise ReuseError("Use a favorite name without control characters or newlines.")
    value = " ".join(unicodedata.normalize("NFC", name).split())
    if not 1 <= len(value) <= 60 or len(value.casefold()) > 60:
        raise ReuseError("Use a favorite name between 1 and 60 characters.")
    if "=" in value or re.fullmatch(r"F[0-9]+(?:v[0-9]+)?", value, re.IGNORECASE):
        raise ReuseError("Choose a name without '=' or a reserved favorite reference such as F1v2.")
    if re.search(
        r"(?:^|\s)(?:x[0-9]+(?:\.[0-9]+)?|today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})$",
        value,
        re.IGNORECASE,
    ) or re.match(
        r"(?:today|yesterday|[0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s|$)",
        value,
        re.IGNORECASE,
    ):
        raise ReuseError("Keep dates and scaling such as x2 out of the favorite name.")
    if any(not c.isprintable() for c in value):
        raise ReuseError("Use a favorite name with printable characters.")
    return value


def normalize_favorite_name(name: str) -> str:
    return favorite_display_name(name).casefold()


def planned_from_meal(meal: "MealSnapshot") -> tuple[PlannedItem, ...]:
    if meal.deleted or not 1 <= len(meal.items) <= 10:
        raise ReuseError("Choose a current, non-deleted meal with one to ten foods.")
    if any(
        item.quantity_method not in {"measured", "approved_estimate"}
        or (item.quantity_method == "approved_estimate" and not item.quantity_basis)
        for item in meal.items
    ):
        raise ReuseError("This meal has incomplete quantity provenance. Correct it first.")
    try:
        return tuple(
            PlannedItem(
                recipe_share=item.recipe_share,
                food_version_id=item.food_version_id,
                edible_milligrams=item.edible_milligrams,
                original_quantity=format(milligrams_to_grams(item.edible_milligrams), "f"),
                original_unit="g",
                estimate_basis=(
                    item.quantity_basis if item.quantity_method == "approved_estimate" else None
                ),
            )
            for item in meal.items
        )
    except (ValidationError, ValueError, ArithmeticError):
        raise ReuseError(
            "This meal has an invalid portion. Correct it before saving a favorite."
        ) from None


def scale_items(items: tuple[PlannedItem, ...], factor: Decimal) -> tuple[PlannedItem, ...]:
    exponent = factor.as_tuple().exponent if isinstance(factor, Decimal) else None
    if (
        not isinstance(factor, Decimal)
        or not factor.is_finite()
        or not 0 < factor <= 100
        or not isinstance(exponent, int)
        or exponent < -3
    ):
        raise ReuseError(
            "Use a scale greater than zero and at most 100, with up to three decimals."
        )
    if not isinstance(items, tuple) or not 1 <= len(items) <= 10:
        raise ReuseError("A favorite must contain one to ten fixed portions.")
    result = []
    for proposed in items:
        try:
            item = PlannedItem.model_validate(proposed.model_dump())
        except (AttributeError, ValidationError, ValueError):
            raise ReuseError(
                "This favorite has an invalid portion; save it again from a meal."
            ) from None
        if item.edible_milligrams is None:
            raise ReuseError("Resolve every portion before saving or scaling a favorite.")
        if item.recipe_share is not None:
            result.append(
                PlannedItem.model_validate(
                    item.model_dump() | {"recipe_share": item.recipe_share.scaled(factor)}
                )
            )
            continue
        with localcontext() as context:
            context.prec = 50
            scaled = Decimal(item.edible_milligrams) * factor
        if scaled != scaled.to_integral_value() or not 1 <= scaled <= 50_000_000:
            raise ReuseError(
                "Choose a scale giving each food an exact milligram amount up to 50 kg."
            )
        mass = int(scaled)
        result.append(
            PlannedItem(
                food_version_id=item.food_version_id,
                edible_milligrams=mass,
                original_quantity=format(milligrams_to_grams(mass), "f"),
                original_unit="g",
                estimate_basis=item.estimate_basis,
            )
        )
    return tuple(result)
